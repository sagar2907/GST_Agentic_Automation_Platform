"""Concrete providers: Gemini, Groq, and a deterministic offline stand-in.

Each provider translates the neutral types in ``llm.types`` into a vendor wire
format and back. Nothing above this module knows which vendor answered.

Tool-call identifiers are minted locally rather than taken from the provider.
Gemini does not return one at all and Groq's is vendor-shaped; since the id is
what an evidence chain cites, it has to be stable and provider-independent or a
Finding replayed from cache would cite an id that no longer exists.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol

import httpx

from gst_recon.llm.types import (
    GenerationConfig,
    LlmResponse,
    Message,
    ProviderError,
    QuotaExhaustedError,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)

# Some providers sit behind a WAF that rejects requests without a conventional
# user agent, which surfaces as an opaque 403 rather than an auth error.
_USER_AGENT = "gst-recon/0.1 (+reconciliation research client)"
_TIMEOUT = httpx.Timeout(180.0, connect=15.0)


class Provider(Protocol):
    """What the agent needs from a model. Deliberately small."""

    name: str
    model: str

    def generate(
        self, messages: list[Message], tools: list[ToolSpec], config: GenerationConfig
    ) -> LlmResponse: ...


def _call_id(step: int, name: str) -> str:
    return f"tc{step:02d}-{name}"


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


class GeminiProvider:
    """Google Generative Language REST client.

    Free-tier quota is dimensioned per project *and per model*, which is why
    the router treats each model id as an independent shard rather than
    round-robining API keys. Measured directly: a 429 names the quota as
    ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier`` with a value of 15.
    """

    name = "gemini"
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, model: str, api_key: str | None = None, client: httpx.Client | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def _payload(
        self, messages: list[Message], tools: list[ToolSpec], config: GenerationConfig
    ) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_text: list[str] = []
        for message in messages:
            if message.role is Role.SYSTEM:
                system_text.append(message.content)
            elif message.role is Role.TOOL:
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": message.tool_name or "tool",
                                    "response": {"result": message.content},
                                }
                            }
                        ],
                    }
                )
            elif message.role is Role.ASSISTANT:
                parts: list[dict[str, Any]] = []
                if message.content:
                    parts.append({"text": message.content})
                for call in message.tool_calls:
                    parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                contents.append({"role": "user", "parts": [{"text": message.content}]})

        generation: dict[str, Any] = {
            "temperature": config.temperature,
            "maxOutputTokens": config.max_output_tokens,
        }
        if config.json_schema is not None:
            generation["responseMimeType"] = "application/json"
            generation["responseSchema"] = config.json_schema

        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_text)}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in tools
                    ]
                }
            ]
        return payload

    def generate(
        self, messages: list[Message], tools: list[ToolSpec], config: GenerationConfig
    ) -> LlmResponse:
        if not self._api_key:
            raise ProviderError("GEMINI_API_KEY is not set")
        url = f"{self.endpoint}/{self.model}:generateContent"
        started = time.perf_counter()
        response = self._client.post(
            url,
            json=self._payload(messages, tools, config),
            headers={
                "x-goog-api-key": self._api_key,
                "content-type": "application/json",
                "user-agent": _USER_AGENT,
            },
        )
        elapsed = time.perf_counter() - started
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise QuotaExhaustedError(f"{self.model}: {response.text[:200]}")
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ProviderError(
                f"{self.model} returned {response.status_code}: {response.text[:300]}"
            )

        body = response.json()
        candidates = body.get("candidates") or [{}]
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        for index, part in enumerate(parts):
            if "text" in part:
                text_chunks.append(part["text"])
            elif "functionCall" in part:
                call = part["functionCall"]
                calls.append(
                    ToolCall(_call_id(index, call["name"]), call["name"], call.get("args") or {})
                )
        meta = body.get("usageMetadata") or {}
        return LlmResponse(
            text="".join(text_chunks).strip(),
            tool_calls=tuple(calls),
            usage=Usage(
                input_tokens=meta.get("promptTokenCount", 0),
                output_tokens=meta.get("candidatesTokenCount", 0) or 0,
                thinking_tokens=meta.get("thoughtsTokenCount", 0) or 0,
            ),
            model=self.model,
            latency_seconds=elapsed,
        )


# --------------------------------------------------------------------------
# Groq
# --------------------------------------------------------------------------


class GroqProvider:
    """Groq's OpenAI-compatible endpoint.

    Kept as a second provider for an independence check rather than for bulk
    work: the measured free tier is 1,000 requests per day but only 8,000
    tokens per minute, and a single bounded investigation consumes roughly
    24,000 tokens, so sustained agent traffic is not viable here.
    """

    name = "groq"
    endpoint = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, model: str, api_key: str | None = None, client: httpx.Client | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def _payload(
        self, messages: list[Message], tools: list[ToolSpec], config: GenerationConfig
    ) -> dict[str, Any]:
        wire: list[dict[str, Any]] = []
        for message in messages:
            if message.role is Role.TOOL:
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": message.content,
                    }
                )
            elif message.role is Role.ASSISTANT and message.tool_calls:
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.content or None,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
            else:
                wire.append({"role": message.role.value, "content": message.content})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": wire,
            "temperature": config.temperature,
            "max_tokens": config.max_output_tokens,
        }
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
        if not self._api_key:
            raise ProviderError("GROQ_API_KEY is not set")
        started = time.perf_counter()
        response = self._client.post(
            self.endpoint,
            json=self._payload(messages, tools, config),
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
                "user-agent": _USER_AGENT,
            },
        )
        elapsed = time.perf_counter() - started
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise QuotaExhaustedError(f"{self.model}: {response.text[:200]}")
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ProviderError(
                f"{self.model} returned {response.status_code}: {response.text[:300]}"
            )

        body = response.json()
        choice = (body.get("choices") or [{}])[0].get("message") or {}
        calls = tuple(
            ToolCall(
                _call_id(index, call["function"]["name"]),
                call["function"]["name"],
                json.loads(call["function"].get("arguments") or "{}"),
            )
            for index, call in enumerate(choice.get("tool_calls") or [])
        )
        usage = body.get("usage") or {}
        return LlmResponse(
            text=(choice.get("content") or "").strip(),
            tool_calls=calls,
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
            model=self.model,
            latency_seconds=elapsed,
        )
