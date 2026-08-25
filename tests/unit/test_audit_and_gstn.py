"""Audit chain and idempotent submission.

These two modules exist for the same reason: a wrong action here creates tax
exposure for a live business, and the system has to be able to say afterwards
what it decided and to guarantee it acted once.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gst_recon.audit import GENESIS, AuditLog, prompt_hash
from gst_recon.domain.taxonomy import ImsAction
from gst_recon.gstn import FakeGspClient, ImsSubmission, SubmitOutcome

AT = datetime(2026, 7, 10, 9, 0, 0, tzinfo=UTC)
GSTIN = "27AAPFU0939F1ZV"


def _log_with(count: int) -> AuditLog:
    log = AuditLog()
    for index in range(count):
        log.append(
            exception_id=f"E{index}",
            action=ImsAction.ACCEPT.value,
            requires_human=False,
            reasons=("within straight-through limits",),
            recorded_at=AT,
            ruleset_version="2026.07",
            amount_at_risk=Decimal("5000.00"),
        )
    return log


def _submission(action: ImsAction = ImsAction.REJECT, document: str = "DOC-1") -> ImsSubmission:
    return ImsSubmission(GSTIN, "07-2026", document, action, "2026.07")


# --- audit chain -----------------------------------------------------------


def test_first_entry_chains_from_genesis() -> None:
    log = _log_with(1)
    assert log.entries[0].previous_digest == GENESIS


def test_chain_verifies_when_intact() -> None:
    assert _log_with(5).verify_chain() == (True, "chain intact")


def test_removing_an_entry_breaks_the_chain() -> None:
    """A deleted row must be detectable, not merely absent.

    An audit log that can lose an entry silently proves nothing about what was
    decided, which is the only thing it exists to prove.
    """
    log = _log_with(5)
    del log.entries[2]
    intact, why = log.verify_chain()
    assert not intact
    assert "sequence" in why or "follow" in why


def test_altering_an_entry_breaks_the_chain() -> None:
    log = _log_with(4)
    tampered = log.entries[1]
    # REJECT, not ACCEPT: the entries are written as ACCEPT, so "tampering"
    # with the same value would leave the digest identical and the test would
    # pass while proving nothing.
    log.entries[1] = type(tampered)(**{**tampered.body(), "action": ImsAction.REJECT.value})
    # Entry 1 now hashes differently, so entry 2 no longer follows it.
    intact, why = log.verify_chain()
    assert not intact
    assert "follow" in why


def test_supersede_preserves_the_original_and_the_chain() -> None:
    """A correction is a new entry, never an edit of the old one."""
    log = _log_with(2)
    replacement = log.append(
        exception_id="E0",
        action=ImsAction.PENDING.value,
        requires_human=True,
        reasons=("reviewer disagreed",),
        recorded_at=AT,
        ruleset_version="2026.07",
    )
    log.supersede(0, replacement)
    assert log.entries[0].action == ImsAction.ACCEPT.value
    assert log.entries[0].superseded_by == replacement.sequence
    assert log.verify_chain()[0]


def test_entries_carry_what_reproduces_the_decision() -> None:
    log = AuditLog()
    entry = log.append(
        exception_id="E1",
        action=ImsAction.ACCEPT.value,
        requires_human=False,
        reasons=("agent proposed ACCEPT",),
        recorded_at=AT,
        ruleset_version="2026.07",
        model="gemini-3.5-flash-lite",
        prompt_digest=prompt_hash("some prompt"),
        confidence=0.91,
        evidence_call_ids=("tc00-query_bank_ledger",),
    )
    assert entry.ruleset_version and entry.model and entry.prompt_digest
    assert entry.evidence_call_ids


def test_prompt_hash_is_stable() -> None:
    assert prompt_hash("abc") == prompt_hash("abc") != prompt_hash("abd")


def test_log_survives_serialisation(tmp_path) -> None:
    path = _log_with(3).write_jsonl(tmp_path / "audit.jsonl")
    assert len([line for line in path.read_text(encoding="utf-8").splitlines() if line]) == 3


# --- idempotent submission -------------------------------------------------


def test_key_is_derived_from_the_decision_not_generated() -> None:
    """The retry must present the same key, or idempotency does nothing.

    A random key would differ on the second attempt -- which is exactly the
    attempt that needs to be recognised as a duplicate.
    """
    assert _submission().idempotency_key() == _submission().idempotency_key()


@pytest.mark.parametrize(
    ("field", "value"),
    [("document_id", "DOC-2"), ("action", ImsAction.ACCEPT), ("ruleset_version", "2026.08")],
)
def test_a_different_decision_gets_a_different_key(field: str, value) -> None:
    base = _submission()
    other = replace(base, **{field: value})
    assert base.idempotency_key() != other.idempotency_key()


def test_first_submit_applies_the_action() -> None:
    client = FakeGspClient()
    result = client.submit_ims_action(_submission())
    assert result.outcome is SubmitOutcome.APPLIED
    assert result.took_effect


def test_resubmitting_replays_rather_than_reapplying() -> None:
    """The core guarantee: the same decision never acts twice."""
    client = FakeGspClient()
    submission = _submission()
    first = client.submit_ims_action(submission)
    second = client.submit_ims_action(submission)

    assert second.outcome is SubmitOutcome.ALREADY_APPLIED
    assert not second.took_effect
    assert second.portal_reference == first.portal_reference
    assert client.calls == 1


def test_replay_does_not_count_as_an_action_taken() -> None:
    """took_effect is what a caller must count, not "no error"."""
    client = FakeGspClient()
    submission = _submission()
    results = [client.submit_ims_action(submission) for _ in range(5)]
    assert sum(1 for result in results if result.took_effect) == 1


def test_duplicate_check_precedes_the_portal_failure() -> None:
    """A replay must succeed even when the portal would refuse fresh work.

    This is the crash case: the action was applied, the connection dropped
    before we heard, and the retry arrives while the portal is unhealthy.
    """
    client = FakeGspClient(fail_after=1)
    submission = _submission()
    client.submit_ims_action(submission)
    replay = client.submit_ims_action(submission)
    assert replay.outcome is SubmitOutcome.ALREADY_APPLIED

    with pytest.raises(ConnectionError):
        client.submit_ims_action(_submission(document="DOC-OTHER"))
