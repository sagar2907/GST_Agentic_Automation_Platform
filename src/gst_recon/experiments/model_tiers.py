"""The model-tier ablation: what does privacy actually cost?

Free hosted models are strong but must never see a real purchase register. A
locally served model is private but weaker. Everyone assumes local models are
"good enough" or "not good enough" without measuring, and the answer decides
something concrete: which decisions a practitioner can run on their own
hardware, and which have to be escalated.

Three metrics, and the third is the one that matters most.

**Resolution rate** is the weakest signal, because it only asks whether a
structurally valid Finding came back.

**Class accuracy** against the generator's ground truth is the obvious one.

**Evidence groundedness** is the metric this experiment exists for. A model can
produce a Finding that passes every mechanical check -- valid JSON, a real
tool_call_id, a fluent rationale -- while asserting something the cited tool
never returned. Observed directly from a local 3B: it cited a genuine bank
ledger call and claimed the ledger "shows a difference of 3414.00 between the
tax amounts", when a bank ledger returns payments and knows nothing about tax
differences. The id was real. The claim was invented.

That failure is strictly more dangerous than an unparseable answer, because an
unparseable answer is rejected and this one is not. So the check here is
mechanical rather than impressionistic: every number appearing in a claim must
also appear in the tool result it cites. A claim that introduces figures its
own evidence never produced is counted ungrounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gst_recon.agents.investigate import Trajectory
from gst_recon.domain.records import Finding
from gst_recon.experiments.stats import Proportion, mean_or_none, wilson

# Numbers with at least two digits. Single digits appear too often by accident
# (array indices, counts) to carry evidence about grounding.
_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")


def _numbers(text: str) -> set[str]:
    found = set()
    for raw in _NUMBER_RE.findall(text):
        cleaned = raw.replace(",", "").rstrip(".")
        if len(cleaned.replace(".", "")) >= 2:
            found.add(cleaned)
            if "." in cleaned:
                found.add(cleaned.split(".")[0])
    return found


def claim_is_grounded(claim: str, tool_result_text: str) -> bool:
    """Does every figure in the claim appear in the evidence it cites?

    Deliberately one-directional. A claim may legitimately summarise, omit, or
    restate qualitatively -- so a claim containing no figures at all is treated
    as grounded, since there is nothing to contradict. What is not legitimate
    is introducing a number the tool never returned, and that is precisely what
    a confabulated evidence chain looks like.
    """
    claimed = _numbers(claim)
    if not claimed:
        return True
    available = _numbers(tool_result_text)
    return claimed <= available


def grounded_fraction(trajectory: Trajectory, finding: Finding) -> float | None:
    """Share of this Finding's claims supported by the tool result they cite."""
    if not finding.evidence:
        return None
    by_call = {step.call_id: str(step.result) for step in trajectory.steps}
    verdicts = [
        claim_is_grounded(item.claim, by_call.get(item.tool_call_id, ""))
        for item in finding.evidence
    ]
    return sum(verdicts) / len(verdicts)


@dataclass(slots=True)
class TierResult:
    """One model tier's performance on the same case set."""

    tier: str
    model: str
    where_usable: str
    cases: int
    resolved: Proportion
    class_correct: Proportion
    fully_grounded: Proportion
    mean_tokens: float | None
    # None when every run was replayed from cache. Reporting a replay latency
    # as though it were a model's speed would make a cached tier look orders of
    # magnitude faster than a freshly-run one and turn a caching artefact into
    # a capability claim.
    mean_seconds: float | None
    freshly_measured: int
    mean_probes: float | None
    repairs_needed: int

    def as_row(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "model": self.model,
            "where_usable": self.where_usable,
            "cases": self.cases,
            "resolved": self.resolved.successes,
            "resolved_rate": round(self.resolved.point, 4),
            "resolved_ci_low": round(self.resolved.low, 4),
            "resolved_ci_high": round(self.resolved.high, 4),
            "class_correct": self.class_correct.successes,
            "class_accuracy": round(self.class_correct.point, 4)
            if self.class_correct.trials
            else None,
            "class_ci_low": round(self.class_correct.low, 4) if self.class_correct.trials else None,
            "fully_grounded": self.fully_grounded.successes,
            "grounded_rate": (
                round(self.fully_grounded.point, 4) if self.fully_grounded.trials else None
            ),
            "grounded_ci_low": (
                round(self.fully_grounded.low, 4) if self.fully_grounded.trials else None
            ),
            "mean_tokens": round(self.mean_tokens, 1) if self.mean_tokens is not None else None,
            "mean_seconds": round(self.mean_seconds, 2) if self.mean_seconds is not None else None,
            "latency_from_n_fresh_runs": self.freshly_measured,
            "mean_probes": round(self.mean_probes, 2) if self.mean_probes is not None else None,
            "repairs_needed": self.repairs_needed,
        }


def summarise_tier(
    tier: str,
    model: str,
    where_usable: str,
    trajectories: list[Trajectory],
    expected_classes: dict[str, str],
) -> TierResult:
    """Reduce one tier's trajectories to reportable proportions."""
    resolved = [path for path in trajectories if path.resolved]
    correct = 0
    grounded = 0
    for path in resolved:
        finding = path.finding
        if finding is None:
            continue
        expected = expected_classes.get(path.exception_id)
        if expected is not None and finding.proposed_class.value == expected:
            correct += 1
        share = grounded_fraction(path, finding)
        if share is not None and share == 1.0:
            grounded += 1

    return TierResult(
        tier=tier,
        model=model,
        where_usable=where_usable,
        cases=len(trajectories),
        resolved=wilson(len(resolved), len(trajectories)),
        class_correct=wilson(correct, len(resolved)),
        fully_grounded=wilson(grounded, len(resolved)),
        mean_tokens=mean_or_none([float(path.usage.total) for path in trajectories]),
        mean_seconds=mean_or_none(
            [path.seconds for path in trajectories if path.live_requests > 0]
        ),
        freshly_measured=sum(1 for path in trajectories if path.live_requests > 0),
        mean_probes=mean_or_none([float(len(path.steps)) for path in trajectories]),
        repairs_needed=sum(1 for path in trajectories if path.live_requests > len(path.steps) + 1),
    )
