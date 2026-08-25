"""The human review queue, and what approving actually means.

This is the control the whole architecture leans on. The instability
measurement is what makes it load-bearing rather than decorative: the same
case, asked three times at temperature zero, proposed ``ACCEPT``, ``REJECT``,
``ACCEPT``. Against an agent that answers both ways to one question, the human
gate is not defence in depth. It is the decision.

Four properties, each of which is a way the gate could be real on paper and
hollow in practice.

**An approval names a person.** An unattributed approval is indistinguishable
from no approval at all six months later, which is when it gets read.

**An approval is bound to what was on the screen.** Every card carries a digest
of exactly what was displayed -- the proposed action, the rationale, the
reasons, the amount, the cited claims. If any of that changed between render
and submit, the approval is refused. Approving text you were never shown is the
failure this prevents, and it needs no attacker: a re-run of the cycle between
loading the page and pressing the button is enough.

**The reviewer's vocabulary is a closed enum.** Approve the proposal, override
it to one of a fixed set of actions, or hold. There is no free-text action and
no way to synthesise one, which is the same structural argument the agent's
tool surface makes.

**The record is written before the portal is touched.** A crash between the two
leaves an approval whose portal outcome is unknown -- discoverable. The other
order leaves an action at the portal that nobody approved on paper.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from gst_recon.audit import AuditEntry, AuditLog
from gst_recon.domain.records import EvidenceItem
from gst_recon.domain.taxonomy import ImsAction
from gst_recon.gstn import ImsSubmission, SubmitOutcome, SubmitResult
from gst_recon.workflow.pipeline import SUBMITTABLE, CycleOutcome

# What a reviewer may put in place of the proposal. A closed set, for the same
# reason the recovery agent's reply kinds are closed: a decision space with no
# free-text member cannot be talked into something nobody enumerated.
OVERRIDES: tuple[ImsAction, ...] = (ImsAction.ACCEPT, ImsAction.REJECT, ImsAction.PENDING)


class ReviewError(Exception):
    """A review action that must not be applied, and why."""


class UnknownItemError(ReviewError):
    """No such item is queued."""


class AlreadyResolvedError(ReviewError):
    """Someone has already decided this one."""


class StaleViewError(ReviewError):
    """The page the approval came from no longer describes the case."""


class UnnamedApproverError(ReviewError):
    """An approval with nobody's name on it."""


class ReviewAction(StrEnum):
    APPROVE = "APPROVE"
    OVERRIDE = "OVERRIDE"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One case awaiting a person, and everything shown to them about it."""

    exception_id: str
    exception_class: str
    supplier_gstin: str
    invoice_number: str
    amount_at_risk: Decimal
    proposed_action: ImsAction
    reasons: tuple[str, ...]
    rationale: str
    confidence: float | None
    evidence: tuple[EvidenceItem, ...]
    audit_sequence: int
    detail: str = ""

    def confirmation(self) -> str:
        """A digest of everything on the card that could change a decision.

        Not a CSRF token -- it does not identify a session. It identifies the
        *content* the reviewer read, so an approval can be checked against what
        was actually in front of them rather than against what the queue holds
        by the time the form arrives.
        """
        parts = [
            self.exception_id,
            self.exception_class,
            self.proposed_action.value,
            f"{self.amount_at_risk:.2f}",
            self.rationale,
            "" if self.confidence is None else f"{self.confidence:.4f}",
            "|".join(self.reasons),
            "|".join(f"{item.tool_call_id}:{item.claim}" for item in self.evidence),
        ]
        return hashlib.sha256("␟".join(parts).encode("utf-8")).hexdigest()[:32]

    @property
    def is_irreversible(self) -> bool:
        """A reject purges the value from 2B for the period."""
        return self.proposed_action is ImsAction.REJECT


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """What a resolved review did."""

    item: ReviewItem
    review_action: ReviewAction
    final_action: ImsAction
    approver: str
    entry: AuditEntry
    submission: SubmitResult | None = None

    @property
    def reached_the_portal(self) -> bool:
        return self.submission is not None


@dataclass(slots=True)
class ReviewQueue:
    """Cases waiting on a person, and the only path from waiting to acted on.

    Held in memory. The audit log is the durable record and it is written
    first, so losing this process loses the worklist but never a decision --
    which is the right way round. A queue that survived while the record of
    what it decided did not would be exactly backwards.
    """

    audit: AuditLog
    client: object
    taxpayer_gstin: str
    return_period: str
    ruleset_version: str
    days_to_cutoff: int = 0
    items: dict[str, ReviewItem] = field(default_factory=dict)
    resolved: dict[str, ReviewOutcome] = field(default_factory=dict)

    def add(self, item: ReviewItem) -> None:
        self.items[item.exception_id] = item

    def pending(self) -> list[ReviewItem]:
        """Highest exposure first. A reviewer with limited time should spend it
        where being wrong costs most, and rejects are irreversible in-cycle."""
        return sorted(
            (item for item in self.items.values() if item.exception_id not in self.resolved),
            key=lambda item: (not item.is_irreversible, -item.amount_at_risk),
        )

    def get(self, exception_id: str) -> ReviewItem:
        item = self.items.get(exception_id)
        if item is None:
            raise UnknownItemError(f"{exception_id} is not in the review queue")
        return item

    @property
    def amount_awaiting_review(self) -> Decimal:
        return sum((item.amount_at_risk for item in self.pending()), Decimal("0.00"))

    def resolve(
        self,
        exception_id: str,
        *,
        approver: str,
        review_action: ReviewAction,
        confirmation: str,
        recorded_at: datetime,
        override: ImsAction | None = None,
        note: str = "",
    ) -> ReviewOutcome:
        """Apply a person's decision: record it, then act on it."""
        item = self.get(exception_id)
        if exception_id in self.resolved:
            raise AlreadyResolvedError(f"{exception_id} was already decided")

        name = approver.strip()
        if not name:
            raise UnnamedApproverError("an approval must carry the name of the person making it")

        if confirmation != item.confirmation():
            raise StaleViewError(
                "this case changed since the page was loaded, so the approval would apply "
                "to something other than what was reviewed; reload and read it again"
            )

        final_action = _final_action(item, review_action, override)
        entry = self.audit.append(
            exception_id=item.exception_id,
            action=final_action.value,
            requires_human=False,
            reasons=(*item.reasons, *_review_reasons(review_action, name, note)),
            recorded_at=recorded_at,
            ruleset_version=self.ruleset_version,
            confidence=item.confidence,
            evidence_call_ids=tuple(evidence.tool_call_id for evidence in item.evidence),
            approver=name,
            amount_at_risk=item.amount_at_risk,
        )
        # The original entry is not rewritten. It keeps its content and its
        # digest, and gains only a pointer forward, so the log still shows what
        # was proposed as well as what was agreed.
        self.audit.supersede(item.audit_sequence, entry)

        submission = None
        if final_action in SUBMITTABLE:
            submission = self.client.submit_ims_action(  # type: ignore[attr-defined]
                ImsSubmission(
                    taxpayer_gstin=self.taxpayer_gstin,
                    return_period=self.return_period,
                    document_id=item.exception_id,
                    action=final_action,
                    ruleset_version=self.ruleset_version,
                )
            )

        outcome = ReviewOutcome(
            item=item,
            review_action=review_action,
            final_action=final_action,
            approver=name,
            entry=entry,
            submission=submission,
        )
        self.resolved[exception_id] = outcome
        return outcome


def _final_action(
    item: ReviewItem, review_action: ReviewAction, override: ImsAction | None
) -> ImsAction:
    if review_action is ReviewAction.APPROVE:
        return item.proposed_action
    if review_action is ReviewAction.HOLD:
        return ImsAction.PENDING
    if override is None or override not in OVERRIDES:
        raise ReviewError(f"an override must name one of {', '.join(a.value for a in OVERRIDES)}")
    return override


def _review_reasons(review_action: ReviewAction, approver: str, note: str) -> tuple[str, ...]:
    reasons = [f"{review_action.value.lower()}d by {approver}"]
    if note.strip():
        # The note is the reviewer's own words and is recorded verbatim. It is
        # never parsed, and nothing downstream branches on it.
        reasons.append(f"reviewer note: {note.strip()}")
    return tuple(reasons)


def queue_from_cycle(outcome: CycleOutcome, *, client, taxpayer_gstin: str, ruleset_version: str):
    """Build a queue from the cases a cycle handed to a person."""
    review = ReviewQueue(
        audit=outcome.audit,
        client=client,
        taxpayer_gstin=taxpayer_gstin,
        return_period=outcome.period,
        ruleset_version=ruleset_version,
        days_to_cutoff=outcome.days_to_cutoff,
    )
    for gated in outcome.gated:
        finding = gated.finding
        exception = gated.exception
        review.add(
            ReviewItem(
                exception_id=exception.exception_id,
                exception_class=exception.exception_class.value,
                supplier_gstin=exception.supplier_gstin,
                invoice_number=_invoice_number(exception),
                amount_at_risk=exception.amount_at_risk,
                proposed_action=gated.decision.action,
                reasons=gated.decision.reasons,
                rationale=finding.rationale if finding else "",
                confidence=finding.confidence if finding else None,
                evidence=finding.evidence if finding else (),
                audit_sequence=gated.audit_sequence,
                detail=exception.detail,
            )
        )
    return review


def _invoice_number(exception) -> str:
    for side in (exception.book, exception.portal):
        if side is not None:
            return side.invoice_number
    return ""


__all__ = [
    "OVERRIDES",
    "AlreadyResolvedError",
    "ReviewAction",
    "ReviewError",
    "ReviewItem",
    "ReviewOutcome",
    "ReviewQueue",
    "StaleViewError",
    "SubmitOutcome",
    "UnknownItemError",
    "UnnamedApproverError",
    "queue_from_cycle",
]
