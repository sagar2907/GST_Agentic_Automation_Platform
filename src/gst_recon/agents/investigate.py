"""Tier 2: the bounded investigation agent.

This is where agency is real. For a hard exception, which probe to run next
depends on what the previous probe returned, and most cases close after one or
two -- so pre-fetching every possible piece of evidence is both possible and
wasteful, which is exactly the shape of problem an agent wins on.

Two design decisions here are load-bearing and easy to get wrong.

**The agent is budget-blind.** The step limit is enforced by this harness and
is never mentioned in the prompt. That costs a little -- an agent told it has
two steps left might prioritise better -- and buys something worth much more:
because behaviour at step k does not depend on the limit, a single run to the
maximum budget *contains* the outcome at every smaller budget as a prefix. The
step-budget curve is then derived by truncating one run instead of running the
whole sweep twelve times, which is the difference between an experiment that
fits in a free tier and one that does not.

**The agent proposes; it never acts.** The return value is a Finding. Whether
anything happens as a result is decided by ``policy.gate``, which is
deterministic code the agent cannot reach.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from gst_recon.agents.tools import ToolSurface
from gst_recon.config import AgentBudgets
from gst_recon.domain.records import EvidenceItem, ExceptionRecord, Finding
from gst_recon.domain.taxonomy import ExceptionClass, ImsAction
from gst_recon.llm.router import Router
from gst_recon.llm.types import GenerationConfig, Message, Role, Usage

SYSTEM_PROMPT = """You investigate exceptions from an Indian GST input-tax-credit
reconciliation. A document appears on one side of the reconciliation and not the
other, or the two sides disagree about it.

Work like a careful accountant: run one probe, read what it returns, and let the
answer decide what to ask next. Most cases resolve after one or two probes. Stop
as soon as the evidence supports a conclusion.

You have read-only tools. You cannot change anything, and you are not being
asked to. Your output is a proposal that a human reviews.

When you have enough evidence, reply with JSON only, no prose around it:
{
  "proposed_class": one of MISSING_IN_2B, MISSING_IN_BOOKS, AMOUNT_MISMATCH,
                    GSTIN_MISMATCH, DUPLICATE, CANCELLED_IRN,
                    CREDIT_NOTE_UNLINKED, RCM, TIME_BARRED,
  "proposed_action": one of ACCEPT, REJECT, PENDING,
  "confidence": a number between 0 and 1,
  "rationale": one or two sentences a reviewer can check,
  "evidence": [{"tool_call_id": "...", "tool_name": "...", "claim": "..."}]
}

Every tool result you receive carries a `tool_call_id`. Every claim in
`evidence` must quote one of those ids exactly as it was given to you.
A conclusion you cannot trace to a tool result is not a conclusion; if the
probes do not settle the question, say so with PENDING and low confidence."""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class TrajectoryStep:
    index: int
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    result: dict[str, Any]


@dataclass(slots=True)
class Trajectory:
    """Everything one investigation did, in order.

    Retained in full even when the run terminated early, because the
    step-budget curve is derived by truncating this rather than by re-running.
    """

    exception_id: str
    exception_class: ExceptionClass
    steps: list[TrajectoryStep] = field(default_factory=list)
    finding: Finding | None = None
    terminated_because: str = "completed"
    usage: Usage = field(default_factory=Usage)
    live_requests: int = 0
    cached_requests: int = 0
    seconds: float = 0.0

    @property
    def tool_sequence(self) -> tuple[str, ...]:
        return tuple(step.tool_name for step in self.steps)

    @property
    def resolved(self) -> bool:
        return self.finding is not None and self.finding.has_articulable_reason


def outcome_at_budget(trajectory: Trajectory, budget: int) -> bool:
    """Would this case have resolved if the step budget had been ``budget``?

    Exact rather than approximate, because the agent never knew its budget: a
    run that emitted its Finding after k probes would have emitted the same
    Finding under any limit of k or more, and would have been cut off under any
    smaller one.
    """
    if not trajectory.resolved:
        return False
    return len(trajectory.steps) <= budget


def _parse_finding(
    text: str, exception: ExceptionRecord, valid_call_ids: set[str], steps_used: int
) -> tuple[Finding | None, str]:
    """Parse and validate the model's JSON verdict.

    Validation is deliberately unforgiving about evidence. A model that invents
    a tool_call_id is confabulating, and a fluent rationale attached to
    fabricated provenance is the single most dangerous output this system can
    produce -- it reads exactly like a good answer.
    """
    found = _JSON_RE.search(text)
    if not found:
        return None, "no JSON object in the reply"
    try:
        payload = json.loads(found.group(0))
    except json.JSONDecodeError as exc:
        return None, f"unparseable JSON: {exc}"

    try:
        proposed_class = ExceptionClass(payload["proposed_class"])
        proposed_action = ImsAction(payload["proposed_action"])
    except (KeyError, ValueError) as exc:
        return None, f"unusable class or action: {exc}"

    evidence: list[EvidenceItem] = []
    for item in payload.get("evidence") or []:
        call_id = str(item.get("tool_call_id", ""))
        if call_id not in valid_call_ids:
            return None, f"evidence cites {call_id!r}, which is not a tool call this run made"
        evidence.append(
            EvidenceItem(call_id, str(item.get("tool_name", "")), str(item.get("claim", "")))
        )

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None, "confidence is not a number"

    return (
        Finding(
            exception_id=exception.exception_id,
            proposed_class=proposed_class,
            proposed_action=proposed_action,
            confidence=max(0.0, min(1.0, confidence)),
            rationale=str(payload.get("rationale", "")).strip(),
            evidence=tuple(evidence),
            steps_used=steps_used,
        ),
        "",
    )


def _describe(exception: ExceptionRecord) -> str:
    lines = [
        f"Exception {exception.exception_id} classified by rules as "
        f"{exception.exception_class.value}.",
        exception.detail,
    ]
    for label, side in (("purchase register", exception.book), ("GSTR-2B", exception.portal)):
        if side is None:
            lines.append(f"No counterpart in the {label}.")
            continue
        lines.append(
            f"In the {label}: invoice {side.invoice_number} dated {side.invoice_date} "
            f"from GSTIN {side.supplier_gstin}, taxable {side.tax.taxable}, "
            f"total tax {side.tax.total_tax}"
            + (f", IRN {side.irn}" if getattr(side, "irn", None) else "")
            + "."
        )
    return "\n".join(line for line in lines if line)


def investigate(
    exception: ExceptionRecord,
    surface: ToolSurface,
    router: Router,
    budgets: AgentBudgets,
    *,
    precedent_hint: str = "",
) -> Trajectory:
    """Run one bounded investigation and return its full trajectory."""
    trajectory = Trajectory(exception.exception_id, exception.exception_class)
    started = time.monotonic()

    user_parts = [_describe(exception)]
    if precedent_hint:
        user_parts.append(f"Similar cases resolved previously:\n{precedent_hint}")
    messages = [
        Message(Role.SYSTEM, SYSTEM_PROMPT),
        Message(Role.USER, "\n\n".join(user_parts)),
    ]

    specs = surface.specs
    config = GenerationConfig(max_output_tokens=700)
    seen_calls: set[tuple[str, str]] = set()
    repeat_count = 0

    while True:
        if time.monotonic() - started > budgets.max_wall_clock_seconds:
            trajectory.terminated_because = "wall_clock_timeout"
            break
        if trajectory.usage.total > budgets.max_tokens_per_case:
            trajectory.terminated_because = "spend_cap"
            break

        # The step budget is checked *after* the model replies, not before.
        # Checking first meant an agent that spent its whole budget on probes
        # was cut off without ever being asked for a verdict -- every probe it
        # ran was then thrown away, and a case that would have resolved at
        # budget k reported as unresolved. The budget bounds how many probes
        # may run, not whether the agent may state what it found.
        response = router.generate(messages, specs, config)
        trajectory.usage = trajectory.usage + response.usage
        trajectory.live_requests += 0 if response.from_cache else 1
        trajectory.cached_requests += 1 if response.from_cache else 0

        if not response.wants_tool:
            finding, why = _parse_finding(
                response.text,
                exception,
                {step.call_id for step in trajectory.steps},
                len(trajectory.steps),
            )
            if finding is None:
                trajectory.terminated_because = f"invalid_finding: {why}"
            else:
                trajectory.finding = finding
                trajectory.terminated_because = "completed"
            break

        if len(trajectory.steps) >= budgets.max_steps:
            trajectory.terminated_because = "step_budget_exhausted"
            break

        call = response.tool_calls[0]
        # The harness mints the call id, not the provider. Gemini returns no
        # identifier at all and Groq's is vendor-shaped, so leaving it to the
        # provider produced colliding ids (every call arrived as "tc00-...",
        # numbered by position within one response rather than by step). An
        # evidence chain built on ambiguous ids cannot be audited.
        call_id = f"tc{len(trajectory.steps):02d}-{call.name}"
        signature = (call.name, json.dumps(call.arguments, sort_keys=True, default=str))
        if signature in seen_calls:
            repeat_count += 1
            if repeat_count >= budgets.loop_repeat_threshold:
                trajectory.terminated_because = "loop_detected"
                break
        seen_calls.add(signature)

        result = surface.invoke(call_id, call.name, call.arguments)
        trajectory.steps.append(
            TrajectoryStep(
                len(trajectory.steps), call.name, call.arguments, call_id, result.payload
            )
        )
        messages = [
            *messages,
            Message(Role.ASSISTANT, response.text, response.tool_calls),
            Message(Role.TOOL, result.render(), tool_call_id=call_id, tool_name=call.name),
        ]

    trajectory.seconds = time.monotonic() - started
    return trajectory
