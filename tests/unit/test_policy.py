"""Policy gate tests.

The gate is the last thing standing between a model's proposal and a tax
filing. Its tests are written adversarially: every case below is a way the
system could act when it should have stopped.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from gst_recon.config import PolicyThresholds
from gst_recon.domain.money import TaxAmounts
from gst_recon.domain.records import EvidenceItem, ExceptionRecord, Finding, Gstr2bLine
from gst_recon.domain.taxonomy import ExceptionClass, ImsAction
from gst_recon.policy.gate import assess_drc01c, days_to_cutoff, decide

POLICY = PolicyThresholds()
GSTIN = "27AAPFU0939F1ZV"


def _exception(cls: ExceptionClass, tax: str = "5000.00") -> ExceptionRecord:
    line = Gstr2bLine(
        "P1",
        GSTIN,
        "INV/2026/1",
        date(2026, 7, 1),
        TaxAmounts(taxable="100000.00", cgst=tax, sgst="0.00"),
    )
    return ExceptionRecord("E1", cls, portal=line)


def _finding(
    action: ImsAction,
    confidence: float = 0.95,
    *,
    evidence: bool = True,
    rationale: str = "bank payment corroborates the document",
) -> Finding:
    items = (EvidenceItem("call-1", "query_bank_ledger", "payment of 118000 on 2026-07-09"),)
    return Finding(
        exception_id="E1",
        proposed_class=ExceptionClass.MISSING_IN_BOOKS,
        proposed_action=action,
        confidence=confidence,
        rationale=rationale,
        evidence=items if evidence else (),
    )


# --- DRC-01C ---------------------------------------------------------------


def test_threshold_is_the_lower_of_the_two_limits() -> None:
    """Rule 88D uses the *lower* of Rs 1 lakh and 20% of available credit.

    Reading it as the higher understates exposure on every small book -- the
    direction that hurts, because the system would report safety while an
    intimation is already inbound.
    """
    small_book = assess_drc01c(Decimal("260000"), Decimal("200000"), POLICY)
    assert small_book.threshold == Decimal("40000.00")  # 20% of 2 lakh, not 1 lakh
    assert small_book.fires

    large_book = assess_drc01c(Decimal("9050000"), Decimal("9000000"), POLICY)
    assert large_book.threshold == Decimal("100000.00")  # capped at 1 lakh
    assert not large_book.fires


def test_exposure_exactly_on_the_threshold_does_not_fire() -> None:
    assessment = assess_drc01c(Decimal("240000"), Decimal("200000"), POLICY)
    assert assessment.excess == assessment.threshold
    assert not assessment.fires


def test_headroom_reports_remaining_slack() -> None:
    assessment = assess_drc01c(Decimal("210000"), Decimal("200000"), POLICY)
    assert assessment.headroom == Decimal("30000.00")


# --- cut-off ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "expected"),
    [(date(2026, 7, 1), 13), (date(2026, 7, 14), 0), (date(2026, 7, 20), -6)],
)
def test_days_to_cutoff(today: date, expected: int) -> None:
    assert days_to_cutoff(today, POLICY) == expected


# --- the gate --------------------------------------------------------------


def test_rule_closable_classes_never_consult_a_finding() -> None:
    decision = decide(_exception(ExceptionClass.CANCELLED_IRN), None, POLICY)
    assert decision.action is ImsAction.REJECT
    assert decision.requires_human


def test_every_reject_requires_a_human() -> None:
    """A reject purges value from 2B for the period; treat it as irreversible."""
    decision = decide(
        _exception(ExceptionClass.MISSING_IN_BOOKS), _finding(ImsAction.REJECT), POLICY
    )
    assert decision.action is ImsAction.REJECT
    assert decision.requires_human
    assert decision.is_irreversible


def test_missing_finding_holds_pending_rather_than_actioning() -> None:
    decision = decide(_exception(ExceptionClass.MISSING_IN_BOOKS), None, POLICY)
    assert decision.action is ImsAction.PENDING
    assert decision.escalated


def test_finding_without_evidence_is_refused() -> None:
    """Never automate a decision that cannot be explained.

    A fluent rationale with no tool-referenced evidence is exactly what a
    confabulating model produces, so the absence of an evidence chain is
    treated as disqualifying rather than merely unfortunate.
    """
    decision = decide(
        _exception(ExceptionClass.MISSING_IN_BOOKS),
        _finding(ImsAction.ACCEPT, evidence=False),
        POLICY,
    )
    assert decision.action is ImsAction.PENDING
    assert decision.escalated
    assert "evidence chain" in " ".join(decision.reasons)


def test_low_confidence_escalates_instead_of_guessing() -> None:
    decision = decide(
        _exception(ExceptionClass.MISSING_IN_BOOKS),
        _finding(ImsAction.ACCEPT, confidence=0.40),
        POLICY,
    )
    assert decision.action is ImsAction.PENDING
    assert decision.escalated


def test_small_accept_passes_straight_through() -> None:
    decision = decide(
        _exception(ExceptionClass.MISSING_IN_BOOKS, tax="5000.00"),
        _finding(ImsAction.ACCEPT),
        POLICY,
    )
    assert decision.action is ImsAction.ACCEPT
    assert not decision.requires_human


def test_large_accept_requires_a_human() -> None:
    """Escalate on blast radius, not on every step."""
    decision = decide(
        _exception(ExceptionClass.MISSING_IN_BOOKS, tax="90000.00"),
        _finding(ImsAction.ACCEPT),
        POLICY,
    )
    assert decision.action is ImsAction.ACCEPT
    assert decision.requires_human


def test_gate_is_pure_with_respect_to_the_clock() -> None:
    """Calling twice must yield identical decisions -- no hidden clock reads."""
    exception = _exception(ExceptionClass.MISSING_IN_BOOKS)
    finding = _finding(ImsAction.ACCEPT)
    assert decide(exception, finding, POLICY) == decide(exception, finding, POLICY)
