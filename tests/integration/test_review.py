"""The gate, end to end: a real cycle, its held-back cases, and a person.

The unit tests build review items by hand, which is the right way to attack the
queue's rules but says nothing about whether a cycle actually hands it anything
sensible. These run the whole pipeline against generated data and then work the
queue the way a reviewer would.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from gst_recon.api import ReviewAction, create_app, queue_from_cycle
from gst_recon.config import load_settings
from gst_recon.data.generator import HARD_MIX, generate
from gst_recon.domain.taxonomy import ImsAction
from gst_recon.gstn import FakeGspClient
from gst_recon.llm import build_router
from gst_recon.workflow import run_cycle

AS_OF = date(2026, 7, 10)
NOW = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)


@pytest.fixture
def reviewed(tmp_path):
    settings = load_settings()
    dataset = generate(
        seed=99,
        period="07-2026",
        base_date=date(2026, 7, 14),
        clean_pairs=60,
        injections=HARD_MIX,
    )
    client = FakeGspClient()
    outcome = run_cycle(
        dataset,
        client,
        build_router(tmp_path, mode="fake"),
        settings,
        as_of=AS_OF,
        recorded_at=NOW,
        limit=25,
    )
    review = queue_from_cycle(
        outcome,
        client=client,
        taxpayer_gstin="27AAPFU0939F1ZV",
        ruleset_version=settings.policy.ruleset_version,
    )
    return outcome, review, client


def test_every_case_the_gate_held_reaches_the_queue(reviewed) -> None:
    outcome, review, _client = reviewed
    assert outcome.queued_for_human > 0, "this dataset should hold something back"
    assert len(review.pending()) == outcome.queued_for_human


def test_nothing_in_the_queue_was_already_sent_to_the_portal(reviewed) -> None:
    """The queue and the auto-submitted set must be disjoint.

    An overlap would mean a case was acted on and then presented for approval,
    which is the gate arriving after the decision it was meant to govern.
    """
    _outcome, review, client = reviewed
    queued = {item.exception_id for item in review.pending()}
    assert queued & set(client.portal_state) == set()


def test_working_the_queue_submits_only_what_was_approved(reviewed) -> None:
    _outcome, review, client = reviewed
    before = client.calls
    pending = review.pending()

    approved = 0
    for item in pending:
        review.resolve(
            item.exception_id,
            approver="A. Reviewer",
            review_action=ReviewAction.APPROVE,
            confirmation=item.confirmation(),
            recorded_at=NOW,
        )
        if item.proposed_action in (ImsAction.ACCEPT, ImsAction.REJECT):
            approved += 1

    assert review.pending() == []
    assert client.calls - before == approved


def test_the_chain_still_verifies_after_a_reviewer_has_worked_it(reviewed) -> None:
    _outcome, review, _client = reviewed
    for item in review.pending():
        review.resolve(
            item.exception_id,
            approver="A. Reviewer",
            review_action=ReviewAction.HOLD,
            confirmation=item.confirmation(),
            recorded_at=NOW,
        )
    assert review.audit.verify_chain() == (True, "chain intact")


def test_every_approval_carries_a_name_and_supersedes_a_proposal(reviewed) -> None:
    _outcome, review, _client = reviewed
    sequences = {item.exception_id: item.audit_sequence for item in review.pending()}
    for item in review.pending():
        review.resolve(
            item.exception_id,
            approver="A. Reviewer",
            review_action=ReviewAction.APPROVE,
            confirmation=item.confirmation(),
            recorded_at=NOW,
        )

    for exception_id, sequence in sequences.items():
        original = review.audit.entries[sequence]
        assert original.superseded_by is not None
        replacement = review.audit.entries[original.superseded_by]
        assert replacement.exception_id == exception_id
        assert replacement.approver == "A. Reviewer"


def test_the_served_queue_renders_every_held_case(reviewed) -> None:
    _outcome, review, _client = reviewed
    http = TestClient(create_app(review, clock=lambda: NOW))

    page = http.get("/").text
    for item in review.pending():
        assert item.exception_id in page

    for item in review.pending():
        detail = http.get(f"/review/{item.exception_id}")
        assert detail.status_code == 200
        assert "<script" not in detail.text.lower()
