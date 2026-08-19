"""The policy gate. Pure functions only -- no I/O, no model calls, no clock.

This module is where legal correctness lives, which is why it is the one place
in the system with a hard architectural rule: every function here takes its
inputs explicitly and returns a value. Nothing here reads the wall clock,
opens a connection, or consults a model.

The reason is testability under adversarial conditions. Cut-off behaviour is
only meaningful relative to a date, and a function that reads ``date.today()``
internally cannot be tested against the day before a deadline. Passing
``as_of`` in makes every statutory boundary a parameter.

Agents propose. This module decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from gst_recon.config import PolicyThresholds
from gst_recon.domain.money import money
from gst_recon.domain.records import ExceptionRecord, Finding
from gst_recon.domain.taxonomy import ExceptionClass, ImsAction
from gst_recon.matching.normalise import financial_year_start


def is_time_barred(invoice_date: date, as_of: date, policy: PolicyThresholds) -> bool:
    """Has the Section 16(4) window for this invoice's financial year closed?

    Credit for a financial year must be claimed by 30 November following the
    end of that year.
    """
    deadline = date(
        financial_year_start(invoice_date) + 1,
        policy.section_16_4_cutoff_month,
        policy.section_16_4_cutoff_day,
    )
    return as_of > deadline


@dataclass(frozen=True, slots=True)
class Drc01cAssessment:
    """Projected exposure to a Rule 88D intimation."""

    claimed_itc: Decimal
    available_itc: Decimal
    excess: Decimal
    threshold: Decimal
    fires: bool

    @property
    def headroom(self) -> Decimal:
        return money(self.threshold - self.excess)


def assess_drc01c(
    claimed_itc: Decimal, available_itc: Decimal, policy: PolicyThresholds
) -> Drc01cAssessment:
    """Assess whether the claimed-versus-available gap would trigger Rule 88D.

    The threshold is the *lower* of a flat rupee amount and a proportion of the
    available credit. Reading "whichever is lower" as "whichever is higher"
    would understate exposure on every small book, which is the direction that
    hurts: it would let the system report safety while a notice is inbound.
    """
    excess = money(claimed_itc - available_itc)
    proportional = money(available_itc * policy.drc01c_relative_fraction)
    threshold = min(policy.drc01c_absolute_inr, proportional)
    return Drc01cAssessment(
        claimed_itc=money(claimed_itc),
        available_itc=money(available_itc),
        excess=excess,
        threshold=threshold,
        fires=excess > threshold,
    )


def days_to_cutoff(as_of: date, policy: PolicyThresholds) -> int:
    """Days remaining until this period's IMS cut-off.

    Negative once the cut-off has passed. Because an unactioned document is
    treated as accepted, a stalled workflow does not fail safe -- it silently
    accepts everything still queued. This number drives the escalation alarm.
    """
    cutoff = date(as_of.year, as_of.month, policy.ims_cutoff_day)
    return (cutoff - as_of).days


@dataclass(frozen=True, slots=True)
class Decision:
    """The output of the gate: what to do, and who has to agree first."""

    exception_id: str
    action: ImsAction
    requires_human: bool
    reasons: tuple[str, ...]
    escalated: bool = False

    @property
    def is_irreversible(self) -> bool:
        """A reject purges the value from 2B for the period. Treat it as final."""
        return self.action is ImsAction.REJECT


# Classes a rule closes outright, with the action the rule dictates.
_RULE_ACTIONS: dict[ExceptionClass, ImsAction] = {
    ExceptionClass.DUPLICATE: ImsAction.REJECT,
    ExceptionClass.CANCELLED_IRN: ImsAction.REJECT,
    ExceptionClass.RCM: ImsAction.PENDING,
    ExceptionClass.TIME_BARRED: ImsAction.PENDING,
}


def decide(  # noqa: PLR0911 -- each early return is one refusal condition; see below
    exception: ExceptionRecord,
    finding: Finding | None,
    policy: PolicyThresholds,
    *,
    confidence_floor: float = 0.75,
) -> Decision:
    """Turn an exception, and optionally an agent's Finding, into an action.

    The ordering of the checks is the safety argument:

    1. Rule-closable classes never consult a model at all.
    2. Absent a Finding, the document is held pending rather than actioned.
    3. A Finding without an articulable, tool-referenced reason is refused --
       never automate a rejection you cannot explain.
    4. Low confidence escalates rather than guessing.
    5. Rejects, and accepts above the value ceiling, require a human.

    The early returns are deliberate and are not collapsed into a single exit.
    Each one is an independent condition under which the system declines to
    act, and each carries its own reason string into the audit record. Folding
    them into nested branches would make the refusal conditions harder to read
    and harder to test in isolation, which is the opposite of what this
    function is for.
    """
    reasons: list[str] = []

    rule_action = _RULE_ACTIONS.get(exception.exception_class)
    if rule_action is not None:
        reasons.append(
            f"{exception.exception_class.value} is closed by rule under ruleset "
            f"{policy.ruleset_version}"
        )
        return Decision(
            exception_id=exception.exception_id,
            action=rule_action,
            # A reject is effectively irreversible within the cycle, so even a
            # rule-driven one goes to a human.
            requires_human=rule_action is ImsAction.REJECT,
            reasons=tuple(reasons),
        )

    if finding is None:
        reasons.append("no finding available; held pending rather than actioned")
        return Decision(
            exception.exception_id, ImsAction.PENDING, True, tuple(reasons), escalated=True
        )

    if not finding.has_articulable_reason:
        reasons.append(
            "finding carries no tool-referenced evidence chain; refusing to "
            "action a decision that cannot be explained"
        )
        return Decision(
            exception.exception_id, ImsAction.PENDING, True, tuple(reasons), escalated=True
        )

    if finding.confidence < confidence_floor:
        reasons.append(f"confidence {finding.confidence:.2f} below floor {confidence_floor:.2f}")
        return Decision(
            exception.exception_id, ImsAction.PENDING, True, tuple(reasons), escalated=True
        )

    action = finding.proposed_action
    reasons.append(f"agent proposed {action.value} with confidence {finding.confidence:.2f}")

    if action is ImsAction.REJECT:
        reasons.append("reject purges the value from 2B for this period; human approval required")
        return Decision(exception.exception_id, action, True, tuple(reasons))

    if action is ImsAction.ACCEPT and exception.amount_at_risk > policy.auto_accept_value_ceiling:
        reasons.append(
            f"tax value {exception.amount_at_risk} exceeds the auto-accept ceiling "
            f"{policy.auto_accept_value_ceiling}"
        )
        return Decision(exception.exception_id, action, True, tuple(reasons))

    reasons.append("within straight-through limits")
    return Decision(exception.exception_id, action, False, tuple(reasons))
