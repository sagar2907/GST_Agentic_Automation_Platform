"""Cache hit accounting.

Regression: the router probes one cache key per shard, and every probe was
counted as a separate lookup. A five-shard router serving a request entirely
from cache reported one hit against five lookups -- a 17% hit rate for a run
that spent no quota at all. Since cache hit rate is one of the numbers the
experiments publish, an accounting artefact here becomes a wrong result.
"""

from __future__ import annotations

from pathlib import Path

from gst_recon.llm import (
    FakeProvider,
    LlmResponse,
    Message,
    ResponseCache,
    Role,
    Router,
    Shard,
)


def _messages(text: str = "MISSING_IN_BOOKS 27AAPFU0939F1ZV") -> list[Message]:
    return [Message(Role.USER, text)]


def test_multi_shard_hit_counts_as_one_lookup(tmp_path: Path) -> None:
    shards = [Shard(FakeProvider(model=f"fake-{i}"), requests_per_minute=1000) for i in range(5)]
    router = Router(shards, tmp_path)

    router.generate(_messages())
    assert router.cache.stats() == {"hits": 0, "misses": 1, "writes": 1, "hit_rate": 0.0}

    router.generate(_messages())
    stats = router.cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5


def test_hit_rate_reaches_one_when_everything_is_cached(tmp_path: Path) -> None:
    shards = [Shard(FakeProvider(model=f"fake-{i}"), requests_per_minute=1000) for i in range(4)]
    warm = Router(shards, tmp_path)
    for index in range(5):
        warm.generate(_messages(f"case {index}"))

    replay = Router(
        [Shard(FakeProvider(model=f"fake-{i}"), requests_per_minute=1000) for i in range(4)],
        tmp_path,
    )
    for index in range(5):
        replay.generate(_messages(f"case {index}"))
    assert replay.cache.stats()["hit_rate"] == 1.0
    assert replay.ledger.live_requests == 0
    assert replay.ledger.cached_requests == 5


def test_response_cached_under_one_shard_is_reused_by_another(tmp_path: Path) -> None:
    """Re-asking a question we already answered is the waste this prevents."""
    cache = ResponseCache(tmp_path)
    first = Router([Shard(FakeProvider(model="fake-a"), requests_per_minute=1000)], cache)
    first.generate(_messages())

    both = Router(
        [
            Shard(FakeProvider(model="fake-b"), requests_per_minute=1000),
            Shard(FakeProvider(model="fake-a"), requests_per_minute=1000),
        ],
        cache,
    )
    response = both.generate(_messages())
    assert response.from_cache
    assert response.model == "fake-a"
    assert both.ledger.live_requests == 0


def test_single_key_lookups_still_account_normally(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    assert cache.get("absent") is None
    cache.put("present", LlmResponse("ok", model="m"))
    assert cache.get("present") is not None
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1
