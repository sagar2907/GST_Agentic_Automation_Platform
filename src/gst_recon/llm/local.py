"""Locally served models, via an Ollama-compatible HTTP endpoint.

This provider exists for two reasons that have nothing to do with capability.

**It is the floor of the fallback chain.** Every hosted free tier can rate-limit
you, and a reconciliation cycle that stalls against a 429 does not fail safely
here -- an unactioned document is treated as accepted at the cut-off. A local
model has no quota, so there is always somewhere for work to go.

**It is the only tier permitted to see real client data.** A purchase register
exposes a business's suppliers, prices and volumes, and the free hosted tiers
state that prompts may be used to improve their products. Anything touching a
real taxpayer runs here or it does not run.

The tradeoff is throughput, not correctness. CPU inference runs at tens of
tokens per second rather than hundreds, which rules out interactive latency and
pushes the whole system towards batch processing -- which is the right shape for
a monthly reconciliation anyway. How much *capability* the privacy costs is not
assumed here; it is measured by the model-tier ablation.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from gst_recon.llm.types import (
    GenerationConfig,
    LlmResponse,
    Message,
    ProviderError,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)

DEFAULT_ENDPOINT = "http://localhost:11434"

# Generous: a small model on CPU can take minutes on a long prompt, and a
# timeout that fires mid-generation looks exactly like a capability failure
# when it is really an impatience failure.
_TIMEOUT = httpx.Timeout(900.0, connect=10.0)


class OllamaProvider:
    """Chat-completions client for a locally served model."""

    name = "local"

    def __init__(
        self,
        model: str = "llama3.2:3b",
        endpoint: str = DEFAULT_ENDPOINT,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def available(self) -> bool:
        """Is a server running with this model pulled?"""
        try:
            response = self._client.get(f"{self.endpoint}/api/tags", timeout=5.0)
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        names = {entry.get("name", "") for entry in response.json().get("models", [])}
        return self.model in names

    def _payload(
        self, messages: list[Message], tools: list[ToolSpec], config: GenerationConfig
    ) -> dict[str, Any]:
        wire: list[dict[str, Any]] = []
        for message in messages:
            if message.role is Role.TOOL:
                # Ollama has no tool_call_id on the result turn, so the id the
                # evidence chain cites travels inside the content, exactly as
                # it does for the hosted providers.
                wire.append({"role": "tool", "content": message.content})
            elif message.role is Role.ASSISTANT and message.tool_calls:
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {"function": {"name": call.name, "arguments": call.arguments}}
                            for call in message.tool_calls
                        ],
                    }
                )
            else:
                wire.append({"role": message.role.value, "content": message.content})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": wire,
            "stream": False,
            "options": {
                "temperature": config.temperature,
                "num_predict": config.max_output_tokens,
            },
        }
        if config.json_schema is not None:
            # Grammar-constrained decoding. Without it a small local model
            # emits fenced markdown wrapping a schema it invented -- it
            # reasons correctly and then fails to say so in a parseable shape,
            # which is indistinguishable from a capability failure unless you
            # test for it. Constraining the grammar separates the two.
            payload["format"] = config.json_schema
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        return payload

    def generate(
        self, messages: list[Message], tools: list[ToolSpec], config: GenerationConfig
    ) -> LlmResponse:
        started = time.perf_counter()
        try:
            response = self._client.post(
                f"{self.endpoint}/api/chat", json=self._payload(messages, tools, config)
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.model}: cannot reach {self.endpoint}: {exc}") from exc
        elapsed = time.perf_counter() - started
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ProviderError(
                f"{self.model} returned {response.status_code}: {response.text[:300]}"
            )

        body = response.json()
        message = body.get("message") or {}
        calls: list[ToolCall] = []
        for index, entry in enumerate(message.get("tool_calls") or []):
            function = entry.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                # Some builds return arguments as a JSON string rather than an
                # object; accept both rather than losing the call.
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            calls.append(
                ToolCall(
                    f"tc{index:02d}-{function.get('name', 'unknown')}",
                    function.get("name", "unknown"),
                    arguments or {},
                )
            )
        return LlmResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=tuple(calls),
            usage=Usage(
                input_tokens=body.get("prompt_eval_count", 0) or 0,
                output_tokens=body.get("eval_count", 0) or 0,
            ),
            model=self.model,
            latency_seconds=elapsed,
        )

    def throughput(self, body: dict[str, Any]) -> float | None:
        """Output tokens per second, when the server reports the timings."""
        produced = body.get("eval_count") or 0
        nanoseconds = body.get("eval_duration") or 0
        if not produced or not nanoseconds:
            return None
        return produced / (nanoseconds / 1e9)
