"""Shard router: cache first, then whichever quota bucket is free.

Free-tier quota on the Generative Language API is dimensioned per project
*and per model*. Measured on this project's key: a 429 names the violated
quota as ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier`` with a value
of 15, and firing 22 requests each at three different models in one minute got
15 + 7 + 15 accepted rather than 15 total.

So the throughput lever is model diversity within one key, not more keys.
That distinction matters: rotating keys across accounts to defeat a per-account
cap is quota evasion and gets keys banned. Spreading load across the dimension
the provider itself uses to define the quota is simply using the documented
allowance.

The router therefore holds several models, spends whichever has budget, and
records what it spent so the experiments can report cost per resolution
honestly.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from gst_recon.llm.cache import ResponseCache, request_fingerprint
from gst_recon.llm.types import (
    GenerationConfig,
    LlmResponse,
    Message,
    ProviderError,
    QuotaExhaustedError,
    ToolSpec,
    Usage,
)

# Measured, not assumed. See the module docstring.
DEFAULT_REQUESTS_PER_MINUTE = 15


@dataclass(slots=True)
class Shard:
    """One model's independent quota bucket."""

    provider: object
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE
    recent: deque[float] = field(default_factory=deque)
    served: int = 0
    rejected: int = 0
    cooldown_until: float = 0.0

    @property
    def model(self) -> str:
        return getattr(self.provider, "model", "unknown")

    def available_at(self, now: float) -> float:
        """Earliest moment this shard could accept a request."""
        if now < self.cooldown_until:
            return self.cooldown_until
        while self.recent and now - self.recent[0] >= 60.0:
            self.recent.popleft()
        if len(self.recent) < self.requests_per_minute:
            return now
        return self.recent[0] + 60.0

    def note_request(self, now: float) -> None:
        self.recent.append(now)
        self.served += 1

    def note_rejection(self, now: float, cooldown_seconds: float = 60.0) -> None:
        self.rejected += 1
        self.cooldown_until = now + cooldown_seconds


@dataclass(slots=True)
class SpendLedger:
    """What a run actually consumed. Reported alongside every experiment."""

    live_requests: int = 0
    cached_requests: int = 0
    usage: Usage = field(default_factory=Usage)
    wall_seconds: float = 0.0
    quota_waits: int = 0

    @property
    def total_requests(self) -> int:
        return self.live_requests + self.cached_requests

    def as_dict(self) -> dict[str, float | int]:
        return {
            "live_requests": self.live_requests,
            "cached_requests": self.cached_requests,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "thinking_tokens": self.usage.thinking_tokens,
            "total_tokens": self.usage.total,
            "wall_seconds": round(self.wall_seconds, 3),
            "quota_waits": self.quota_waits,
        }


class Router:
    """Cache-first dispatch across independent per-model quota buckets."""

    def __init__(
        self,
        shards: list[Shard],
        cache: ResponseCache | Path | str,
        *,
        sleeper=time.sleep,
        clock=time.monotonic,
        max_wait_seconds: float = 90.0,
        bypass_cache: bool = False,
    ) -> None:
        if not shards:
            raise ValueError("router needs at least one shard")
        self.shards = shards
        # Bypassing the cache is what makes run-to-run variance measurable.
        # The cache is normally the point -- it makes results reproducible and
        # re-runs free -- but reproducibility is not the same as stability.
        # Temperature 0 does not make a model deterministic, so a cached single
        # draw looks perfectly repeatable while hiding how much the underlying
        # answer moves. Measuring that requires deliberately asking twice.
        self.bypass_cache = bypass_cache
        self.cache = cache if isinstance(cache, ResponseCache) else ResponseCache(Path(cache))
        self.ledger = SpendLedger()
        self._sleep = sleeper
        self._now = clock
        self._max_wait = max_wait_seconds
        self._cursor = 0

    def _ordered_shards(self) -> list[Shard]:
        """Round-robin start point, so load spreads instead of hammering one."""
        order = self.shards[self._cursor :] + self.shards[: self._cursor]
        self._cursor = (self._cursor + 1) % len(self.shards)
        return order

    def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        config: GenerationConfig | None = None,
    ) -> LlmResponse:
        tools = tools or []
        config = config or GenerationConfig()

        # One candidate key per shard, because the model id is part of the key.
        # The cache counts this as a single lookup rather than N, or a request
        # served entirely from cache would report a 1/N hit rate.
        candidate_keys = [
            request_fingerprint(
                provider=getattr(shard.provider, "name", "unknown"),
                model=shard.model,
                messages=messages,
                tools=tools,
                config=config,
            )
            for shard in self.shards
        ]
        if not self.bypass_cache:
            cached = self.cache.get_any(candidate_keys)
            if cached is not None:
                self.ledger.cached_requests += 1
                return cached

        last_error: Exception | None = None
        for shard in self._ordered_shards():
            now = self._now()
            ready_at = shard.available_at(now)
            if ready_at > now:
                wait = ready_at - now
                if wait > self._max_wait:
                    continue
                self.ledger.quota_waits += 1
                self._sleep(wait)
                now = self._now()

            fingerprint = request_fingerprint(
                provider=getattr(shard.provider, "name", "unknown"),
                model=shard.model,
                messages=messages,
                tools=tools,
                config=config,
            )
            shard.note_request(now)
            try:
                response = shard.provider.generate(messages, tools, config)
            except QuotaExhaustedError as exc:
                # The provider disagreed with our local accounting. Trust the
                # provider, cool this shard off, and try the next one.
                shard.note_rejection(self._now())
                last_error = exc
                continue
            except ProviderError as exc:
                last_error = exc
                continue

            # Still written when bypassing, so a variance run warms the cache
            # for later reproducible runs rather than throwing the work away.
            self.cache.put(fingerprint, response)
            self.ledger.live_requests += 1
            self.ledger.usage = self.ledger.usage + response.usage
            self.ledger.wall_seconds += response.latency_seconds
            return response

        raise QuotaExhaustedError(
            f"every shard is rate limited or failing; last error: {last_error}"
        ) from last_error

    def stats(self) -> dict[str, object]:
        return {
            "spend": self.ledger.as_dict(),
            "cache": self.cache.stats(),
            "shards": [
                {"model": shard.model, "served": shard.served, "rejected": shard.rejected}
                for shard in self.shards
            ],
        }
