"""The reconciliation cycle, as ordinary orchestration.

Deliberately free of any durability machinery. Everything here is a plain
function over plain data, so the whole cycle can be exercised offline with no
database, no model and no portal -- and so the durable wrapper in
``workflow.durable`` has something small and already-correct to wrap.

The stage order encodes the safety argument:

1. Reconcile deterministically. Most documents never reach a model.
2. Route the residue. Rule-closable classes never consult one either.
3. Investigate what is left, under a bounded agent.
4. Gate. Deterministic code decides; the agent only proposed.
5. Record the decision *before* acting on it.
6. Submit, idempotently.
7. Record the outcome.

Step 5 comes before step 6 on purpose. If the process dies between them, the
audit says a decision was taken and the portal may or may not have applied it,
which is a discrepancy a human can find. The other order loses the decision
entirely and leaves an action nobody can explain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from gst_recon.agents.investigate import investigate
from gst_recon.agents.tools import ToolSurface
from gst_recon.audit import AuditLog, prompt_hash
from gst_recon.config import Settings
from gst_recon.domain.records import ExceptionRecord
from gst_recon.domain.taxonomy import DEFAULT_ROUTING, ImsAction, Tier
from gst_recon.gstn import ImsSubmission, SubmitOutcome, SubmitResult
from gst_recon.llm.router import Router
from gst_recon.matching.engine import reconcile
from gst_recon.policy.gate import Decision, days_to_cutoff, decide

# Actions that change something at the portal. PENDING and NO_ACTION are not
# submitted: PENDING is the absence of a decision, and NO_ACTION is what
# happens when nobody acts at all.
SUBMITTABLE = (ImsAction.ACCEPT, ImsAction.REJECT)


@dataclass(slots=True)
class CycleOutcome:
    period: str
    documents: int
    exact_matches: int
    fuzzy_matches: int
    exceptions: int
    rule_closed: int
    investigated: int
    submitted: int
    replayed: int
    queued_for_human: int
    days_to_cutoff: int
    audit: AuditLog = field(default_factory=AuditLog)
    decisions: list[Decision] = field(default_factory=list)
    submissions: list[SubmitResult] = field(default_factory=list)

    @property
    def exactly_once_violations(self) -> int:
        """Distinct idempotency keys that took effect more than once.

        The headline safety number. Anything above zero means the portal
        applied the same action twice, which for a reject is not recoverable
        inside the filing cycle.
        """
        seen: dict[str, int] = {}
        for result in self.submissions:
            if result.took_effect:
                seen[result.idempotency_key] = seen.get(result.idempotency_key, 0) + 1
        return sum(1 for count in seen.values() if count > 1)


def route(exception: ExceptionRecord) -> Tier:
    return DEFAULT_ROUTING[exception.exception_class]


def run_cycle(
    dataset,
    client,
    router: Router,
    settings: Settings,
    *,
    as_of: date,
    recorded_at: datetime,
    taxpayer_gstin: str = "27AAPFU0939F1ZV",
    limit: int | None = None,
    approver: str | None = None,
) -> CycleOutcome:
    """Run one filing cycle end to end.

    ``as_of`` and ``recorded_at`` are parameters rather than clock reads so the
    cut-off arithmetic and the audit ordering can both be tested against a
    frozen clock. Cut-off behaviour is the one thing here that cannot be
    verified any other way.
    """
    result = reconcile(
        dataset.books,
        dataset.portal,
        tolerances=settings.tolerances,
        policy=settings.policy,
        as_of=as_of,
    )
    exceptions = result.exceptions[:limit] if limit else result.exceptions

    outcome = CycleOutcome(
        period=dataset.period,
        documents=result.total_documents,
        exact_matches=result.exact_count,
        fuzzy_matches=result.fuzzy_count,
        exceptions=len(exceptions),
        rule_closed=0,
        investigated=0,
        submitted=0,
        replayed=0,
        queued_for_human=0,
        days_to_cutoff=days_to_cutoff(as_of, settings.policy),
    )

    surface = ToolSurface(dataset, settings.tolerances)

    for exception in exceptions:
        finding = None
        model = ""
        digest = ""
        if route(exception) is Tier.INVESTIGATION:
            trajectory = investigate(exception, surface, router, settings.budgets)
            finding = trajectory.finding
            outcome.investigated += 1
            model = next((shard.model for shard in router.shards if shard.served), "")
            digest = prompt_hash(exception.detail)
        else:
            outcome.rule_closed += 1

        decision = decide(exception, finding, settings.policy)
        outcome.decisions.append(decision)

        # Recorded before anything is submitted. A crash after this point
        # leaves a decision with an unknown portal outcome, which is
        # discoverable; a crash before it would leave the reverse.
        outcome.audit.append(
            exception_id=exception.exception_id,
            action=decision.action.value,
            requires_human=decision.requires_human,
            reasons=decision.reasons,
            recorded_at=recorded_at,
            ruleset_version=settings.policy.ruleset_version,
            model=model,
            prompt_digest=digest,
            confidence=finding.confidence if finding else None,
            evidence_call_ids=tuple(item.tool_call_id for item in finding.evidence)
            if finding
            else (),
            approver=approver if not decision.requires_human else None,
            amount_at_risk=exception.amount_at_risk,
        )

        if decision.requires_human:
            outcome.queued_for_human += 1
            continue
        if decision.action not in SUBMITTABLE:
            continue

        submission = ImsSubmission(
            taxpayer_gstin=taxpayer_gstin,
            return_period=dataset.period,
            document_id=exception.exception_id,
            action=decision.action,
            ruleset_version=settings.policy.ruleset_version,
        )
        submitted = client.submit_ims_action(submission)
        outcome.submissions.append(submitted)
        if submitted.outcome is SubmitOutcome.APPLIED:
            outcome.submitted += 1
        elif submitted.outcome is SubmitOutcome.ALREADY_APPLIED:
            outcome.replayed += 1

    return outcome
