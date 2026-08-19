"""Provider-neutral model access: types, cache, shard router, offline fake."""

from __future__ import annotations

import os
from pathlib import Path

from gst_recon.llm.cache import ResponseCache, request_fingerprint
from gst_recon.llm.fake import REFERENCE_PROBES, FakeProvider
from gst_recon.llm.providers import GeminiProvider, GroqProvider, Provider
from gst_recon.llm.router import Router, Shard, SpendLedger
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

__all__ = [
    "REFERENCE_PROBES",
    "FakeProvider",
    "GeminiProvider",
    "GenerationConfig",
    "GroqProvider",
    "LlmResponse",
    "Message",
    "Provider",
    "ProviderError",
    "QuotaExhaustedError",
    "ResponseCache",
    "Role",
    "Router",
    "Shard",
    "SpendLedger",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "build_router",
    "request_fingerprint",
]

# Ordered by measured quality-per-second on a realistic tool-calling payload.
# gemini-3.5-flash reasons before answering and picks the right probe; the
# lite models are faster and equally accurate on straightforward cases. The
# two slower entries exist purely as additional quota buckets -- they absorb
# spillover once the fast ones are saturated.
GEMINI_SHARD_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
)

# Deliberately excluded, with reasons, so nobody re-adds them hopefully:
#   gemini-3.6-flash        median 67s and it failed to emit a tool call at all
#   gemini-3.7-flash        503 "high demand" under any concurrency
#   gemini-2.5-flash(-lite) 404 "no longer available to new users"
EXCLUDED_MODELS: dict[str, str] = {
    "gemini-3.6-flash": "median 67s; did not emit a tool call on the probe payload",
    "gemini-3.7-flash": "503 high demand under concurrency",
    "gemini-2.5-flash": "404 no longer available to new API keys",
    "gemini-2.5-flash-lite": "404 no longer available to new API keys",
}


def build_router(
    cache_dir: Path | str,
    *,
    mode: str = "fake",
    models: tuple[str, ...] = GEMINI_SHARD_MODELS,
) -> Router:
    """Assemble a router for the requested mode.

    ``fake`` is the default everywhere except an explicit live run, so a
    missing key can never turn into a silent live spend, and the test suite has
    no way to reach the network by accident.
    """
    if mode == "fake":
        return Router([Shard(FakeProvider(), requests_per_minute=10**6)], cache_dir)
    if mode == "live":
        shards = [Shard(GeminiProvider(model)) for model in models]
        if os.environ.get("GROQ_API_KEY"):
            # A genuinely separate provider, so a genuinely separate bucket --
            # but a tight token-per-minute ceiling, hence the low rpm.
            shards.append(Shard(GroqProvider("openai/gpt-oss-20b"), requests_per_minute=4))
        return Router(shards, cache_dir)
    raise ValueError(f"unknown llm mode {mode!r}; expected 'fake' or 'live'")
