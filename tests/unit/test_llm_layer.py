"""Cache, router and offline-provider tests.

Everything here runs without a network or a key. That is the point: if the
suite could reach a provider, it would be neither reproducible nor free, and
it would stop working the day a quota changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gst_recon.llm import (
    FakeProvider,
    GenerationConfig,
    LlmResponse,
    Message,
    QuotaExhaustedError,
    ResponseCache,
    Role,
    Router,
    Shard,
    ToolCall,
    ToolSpec,
    Usage,
    build_router,
    request_fingerprint,
)

TOOLS = [
    ToolSpec(
        "query_purchase_register",
        "Search book lines.",
        {"type": "object", "properties": {"gstin": {"type": "string"}}},
    ),
    ToolSpec(
        "query_bank_ledger",
        "Find payments.",
        {"type": "object", "properties": {"gstin": {"type": "string"}}},
    ),
    ToolSpec(
        "query_vendor_master",
        "Vendor record.",
        {"type": "object", "properties": {"gstin": {"type": "string"}}},
    ),
]


def _messages(text: str = "MISSING_IN_BOOKS for 27AAPFU0939F1ZV") -> list[Message]:
    return [Message(Role.SYSTEM, "You investigate."), Message(Role.USER, text)]


# --- fingerprinting --------------------------------------------------------


def test_identical_requests_share_a_fingerprint() -> None:
    args = {
        "provider": "gemini",
        "model": "m",
        "messages": _messages(),
        "tools": TOOLS,
        "config": GenerationConfig(),
    }
    assert request_fingerprint(**args) == request_fingerprint(**args)


@pytest.mark.parametrize(
    "mutation",
    ["provider", "model", "messages", "tools", "config"],
    ids=["provider", "model", "messages", "tools", "config"],
)
def test_every_input_that_changes_the_answer_changes_the_key(mutation: str) -> None:
    """A key that ignores a field would replay an answer to a different question."""
    base = {
        "provider": "gemini",
        "model": "m",
        "messages": _messages(),
        "tools": TOOLS,
        "config": GenerationConfig(),
    }
    other = dict(base)
    if mutation == "provider":
        other["provider"] = "groq"
    elif mutation == "model":
        other["model"] = "m2"
    elif mutation == "messages":
        other["messages"] = _messages("AMOUNT_MISMATCH for 27AAPFU0939F1ZV")
    elif mutation == "tools":
        other["tools"] = TOOLS[:1]
    else:
        other["config"] = GenerationConfig(max_output_tokens=99)
    assert request_fingerprint(**base) != request_fingerprint(**other)


# --- cache -----------------------------------------------------------------


def test_cache_round_trips_a_response(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    response = LlmResponse(
        text="ok",
        tool_calls=(ToolCall("tc00-x", "query_bank_ledger", {"gstin": "27AAPFU0939F1ZV"}),),
        usage=Usage(120, 30, 5),
        model="m",
        latency_seconds=2.5,
    )
    cache.put("abc123", response)
    restored = cache.get("abc123")
    assert restored is not None
    assert restored.text == "ok"
    assert restored.tool_calls[0].arguments == {"gstin": "27AAPFU0939F1ZV"}
    assert restored.usage.total == 155


def test_replayed_response_reports_zero_latency(tmp_path: Path) -> None:
    """A cached run must not masquerade as a live one in the results."""
    cache = ResponseCache(tmp_path)
    cache.put("k", LlmResponse("ok", model="m", latency_seconds=4.2))
    restored = cache.get("k")
    assert restored is not None
    assert restored.from_cache
    assert restored.latency_seconds == 0.0


def test_missing_entry_counts_as_a_miss(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    assert cache.get("nope") is None
    assert cache.stats()["misses"] == 1


def test_get_any_records_a_single_miss_across_many_keys(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    assert cache.get_any(["a" * 64, "b" * 64, "c" * 64]) is None
    assert cache.stats()["misses"] == 1


def test_partial_writes_are_never_readable(tmp_path: Path) -> None:
    """An interrupted write must not leave a replayable half-entry."""
    cache = ResponseCache(tmp_path)
    cache.put("k" * 64, LlmResponse("ok", model="m"))
    assert list(tmp_path.rglob("*.partial")) == []


# --- router ----------------------------------------------------------------


def test_second_identical_request_is_served_from_cache(tmp_path: Path) -> None:
    router = Router([Shard(FakeProvider(), requests_per_minute=1000)], tmp_path)
    first = router.generate(_messages(), TOOLS)
    second = router.generate(_messages(), TOOLS)
    assert not first.from_cache
    assert second.from_cache
    assert router.ledger.live_requests == 1
    assert router.ledger.cached_requests == 1


def test_router_spreads_load_across_shards(tmp_path: Path) -> None:
    shards = [Shard(FakeProvider(model=f"fake-{i}"), requests_per_minute=1000) for i in range(3)]
    router = Router(shards, tmp_path)
    for index in range(9):
        router.generate(_messages(f"MISSING_IN_BOOKS case {index}"), TOOLS)
    assert [shard.served for shard in shards] == [3, 3, 3]


def test_shard_stops_accepting_once_its_minute_budget_is_spent() -> None:
    shard = Shard(FakeProvider(), requests_per_minute=2)
    shard.note_request(100.0)
    shard.note_request(100.5)
    assert shard.available_at(101.0) == pytest.approx(160.0)


def test_budget_frees_up_after_the_window_rolls() -> None:
    shard = Shard(FakeProvider(), requests_per_minute=2)
    shard.note_request(100.0)
    shard.note_request(100.5)
    assert shard.available_at(161.0) == 161.0


def test_router_waits_rather_than_failing_when_a_shard_is_briefly_full(tmp_path: Path) -> None:
    slept: list[float] = []
    clock = {"t": 0.0}

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    router = Router(
        [Shard(FakeProvider(), requests_per_minute=1)],
        tmp_path,
        sleeper=fake_sleep,
        clock=lambda: clock["t"],
    )
    router.generate(_messages("case one"), TOOLS)
    router.generate(_messages("case two"), TOOLS)
    assert slept and slept[0] == pytest.approx(60.0)
    assert router.ledger.quota_waits == 1


def test_router_raises_when_no_shard_can_serve_within_the_wait_ceiling(tmp_path: Path) -> None:
    clock = {"t": 0.0}
    router = Router(
        [Shard(FakeProvider(), requests_per_minute=1)],
        tmp_path,
        sleeper=lambda _: None,
        clock=lambda: clock["t"],
        max_wait_seconds=5.0,
    )
    router.generate(_messages("only one"), TOOLS)
    with pytest.raises(QuotaExhaustedError):
        router.generate(_messages("second one"), TOOLS)


def test_default_mode_is_offline() -> None:
    """A missing key must never silently become a live spend."""
    router = build_router("/tmp/does-not-matter")
    assert router.shards[0].provider.name == "fake"


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown llm mode"):
        build_router("/tmp/x", mode="production")


# --- the offline provider --------------------------------------------------


def test_fake_provider_is_deterministic() -> None:
    first = FakeProvider().generate(_messages(), TOOLS, GenerationConfig())
    second = FakeProvider().generate(_messages(), TOOLS, GenerationConfig())
    assert (first.text, first.tool_calls) == (second.text, second.tool_calls)


def test_fake_provider_follows_the_reference_probe_order() -> None:
    provider = FakeProvider()
    history = _messages()
    first = provider.generate(history, TOOLS, GenerationConfig())
    assert first.tool_calls[0].name == "query_purchase_register"

    history = [
        *history,
        Message(Role.ASSISTANT, first.text, first.tool_calls),
        Message(
            Role.TOOL,
            "no candidates",
            tool_call_id="tc00-query_purchase_register",
            tool_name="query_purchase_register",
        ),
    ]
    second = provider.generate(history, TOOLS, GenerationConfig())
    assert second.tool_calls[0].name == "query_bank_ledger"


def test_fake_provider_eventually_concludes() -> None:
    provider = FakeProvider()
    history = _messages()
    for step in range(6):
        response = provider.generate(history, TOOLS, GenerationConfig())
        if not response.wants_tool:
            assert '"proposed_action"' in response.text
            return
        history = [
            *history,
            Message(Role.ASSISTANT, response.text, response.tool_calls),
            Message(
                Role.TOOL,
                "result",
                tool_call_id=f"tc{step:02d}",
                tool_name=response.tool_calls[0].name,
            ),
        ]
    pytest.fail("provider never produced a finding")


def test_failure_modes_are_reproducible_on_demand() -> None:
    looping = FakeProvider(failure_mode="loop").generate(_messages(), TOOLS, GenerationConfig())
    assert looping.tool_calls[0].call_id == "tc-loop"

    hollow = FakeProvider(failure_mode="no_evidence").generate(
        _messages(), TOOLS, GenerationConfig()
    )
    assert '"evidence": []' in hollow.text
