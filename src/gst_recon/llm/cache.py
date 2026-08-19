"""Content-addressed cache for provider responses.

This is the single most consequential piece of infrastructure in the project,
and it exists because of a hard constraint: the free tier allows 15 requests
per minute per model, so quota -- not money -- is the scarce resource.

Three things follow from caching on the exact content of a request:

* Re-running an experiment costs nothing. Without this, every re-run of the
  ablation would spend a fresh day of quota, and nobody iterates on an
  experiment they can only run once.
* Published results are reproducible from a clean clone with no API key at
  all, because the cache is committed alongside the code.
* Trajectories are deterministic, which is what makes the step-budget curve
  derivable by truncating a single long run rather than running twelve.

The key covers everything that can change the answer: provider, model,
messages, tool schemas, and generation config. Getting that wrong in the
permissive direction -- omitting a field that influences output -- would
silently replay an answer to a different question, so the key is built from a
canonical JSON encoding with sorted keys rather than from Python's hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from gst_recon.llm.types import (
    GenerationConfig,
    LlmResponse,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
)

CACHE_FORMAT_VERSION = 1


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def request_fingerprint(
    *,
    provider: str,
    model: str,
    messages: list[Message],
    tools: list[ToolSpec],
    config: GenerationConfig,
) -> str:
    """Hash everything that could change the response.

    The model name is included even though shards are interchangeable from the
    router's point of view: two models given the same prompt are two different
    experiments, and conflating them would corrupt any cross-model comparison.
    """
    payload = {
        "version": CACHE_FORMAT_VERSION,
        "provider": provider,
        "model": model,
        "messages": [asdict(message) for message in messages],
        "tools": [asdict(tool) for tool in tools],
        "config": asdict(config),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ResponseCache:
    """Disk-backed cache. One JSON file per request, sharded by hash prefix.

    Files rather than a database because these are committed to the repository
    and reviewed in diffs: a reviewer can open the exact prompt and the exact
    response that produced a published number.
    """

    root: Path
    hits: int = 0
    misses: int = 0
    writes: int = 0

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def _path(self, fingerprint: str) -> Path:
        return self.root / fingerprint[:2] / f"{fingerprint}.json"

    def get_any(self, fingerprints: list[str]) -> LlmResponse | None:
        """Return the first cached response among several candidate keys.

        The router probes one key per shard, because the model id is part of
        the key. Counting each of those probes as a separate cache lookup would
        report a hit rate of 1/N for a request that was in fact served entirely
        from cache, so hit accounting happens here -- once per request -- rather
        than once per key.

        Returning a response cached under a *different* shard than the one the
        router would have chosen is deliberate: the prompt is identical, and
        spending live quota to re-ask a question we already have an answer to
        is the exact waste this cache exists to prevent.
        """
        for fingerprint in fingerprints:
            found = self._load(fingerprint)
            if found is not None:
                self.hits += 1
                return found
        self.misses += 1
        return None

    def get(self, fingerprint: str) -> LlmResponse | None:
        found = self._load(fingerprint)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return found

    def _load(self, fingerprint: str) -> LlmResponse | None:
        path = self._path(fingerprint)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LlmResponse(
            text=raw["text"],
            tool_calls=tuple(
                ToolCall(call["call_id"], call["name"], call["arguments"])
                for call in raw["tool_calls"]
            ),
            usage=Usage(**raw["usage"]),
            model=raw["model"],
            from_cache=True,
            # Latency is deliberately zeroed on replay. Reporting the original
            # wall clock would make a cached run look like a live one in the
            # results, which is exactly the sort of quiet fiction this project
            # is supposed to avoid.
            latency_seconds=0.0,
        )

    def put(self, fingerprint: str, response: LlmResponse) -> None:
        path = self._path(fingerprint)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_FORMAT_VERSION,
            "text": response.text,
            "tool_calls": [asdict(call) for call in response.tool_calls],
            "usage": asdict(response.usage),
            "model": response.model,
            "live_latency_seconds": round(response.latency_seconds, 4),
        }
        # Written via a temporary file and renamed, so an interrupted run can
        # never leave a half-written entry that would later be replayed as if
        # it were a complete response.
        temporary = path.with_suffix(".partial")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
        self.writes += 1

    @property
    def hit_rate(self) -> float:
        looked_up = self.hits + self.misses
        return self.hits / looked_up if looked_up else 0.0

    def stats(self) -> dict[str, float | int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": round(self.hit_rate, 4),
        }
