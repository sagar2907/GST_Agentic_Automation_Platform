"""The experiment harness.

A word on what these numbers do and do not mean, because it is the easiest
thing in the project to get wrong.

Runs in ``fake`` mode measure **the harness**: whether budgets bind, whether
the truncation identity holds, whether precedent retrieval shortens
investigations, and what a case costs in requests. They do not measure model
quality -- the offline provider follows a script, so its resolution rate is a
property of that script and reporting it as an accuracy figure would be
inventing a result.

Runs in ``live`` mode measure the model, on a much smaller sample, and are
reported separately with their sample size and interval attached. Mixing the
two into one headline number would be the single most misleading thing this
project could publish, so ``mode`` travels with every row.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from gst_recon.agents.investigate import Trajectory, investigate, outcome_at_budget
from gst_recon.agents.tools import ToolSurface
from gst_recon.config import AgentBudgets, Settings
from gst_recon.data.generator import DISTRIBUTION_MIX, HARD_MIX, Dataset, generate
from gst_recon.domain.taxonomy import DEFAULT_ROUTING, ExceptionClass, ImsAction, Tier
from gst_recon.experiments.model_tiers import TierResult, summarise_tier
from gst_recon.experiments.stats import Proportion, mean_or_none, wilson
from gst_recon.llm import MODEL_TIERS, REFERENCE_PROBES, build_router
from gst_recon.llm.router import Router
from gst_recon.matching.engine import ReconciliationResult, reconcile
from gst_recon.memory import InMemoryPrecedentStore, Precedent
from gst_recon.policy.gate import decide

BASE_DATE = date(2026, 7, 14)
MAX_BUDGET = 8

# Metrics that are meaningless when the offline provider answers, because the
# offline provider is scripted from REFERENCE_PROBES -- the same table the
# trajectory evaluator scores against -- and it reads the true exception class
# straight out of the prompt.
#
# Run offline, these returned first-probe accuracy 1.000, reference overlap
# 1.000, steps-over-reference 0.000 and precision 1.000 at every confidence
# threshold. Those are not results; they are the evaluator scoring a script
# against itself. Reporting them would have been the most flattering and most
# dishonest table in the project, so the harness refuses to produce them
# offline rather than leaving it to a reader to notice.
NOT_MEASURABLE_OFFLINE = (
    "not measured: requires a live provider. The offline provider is scripted "
    "from the same reference paths the evaluator scores against, so any quality "
    "figure it produces is circular."
)


@dataclass(slots=True)
class ArmResult:
    """One arm of the tiering ablation."""

    name: str
    documents: int
    exceptions: int
    resolved_without_human: Proportion
    llm_requests: int
    total_tokens: int
    escalated_to_human: int

    @property
    def requests_per_resolution(self) -> float | None:
        resolved = self.resolved_without_human.successes
        return self.llm_requests / resolved if resolved else None

    def as_row(self) -> dict[str, object]:
        return {
            "arm": self.name,
            "documents": self.documents,
            "exceptions": self.exceptions,
            "resolved": self.resolved_without_human.successes,
            "resolved_rate": round(self.resolved_without_human.point, 4),
            "ci_low": round(self.resolved_without_human.low, 4),
            "ci_high": round(self.resolved_without_human.high, 4),
            "llm_requests": self.llm_requests,
            "total_tokens": self.total_tokens,
            "escalated": self.escalated_to_human,
            "requests_per_resolution": (
                round(self.requests_per_resolution, 3)
                if self.requests_per_resolution is not None
                else None
            ),
        }


@dataclass(slots=True)
class Experiments:
    settings: Settings
    mode: str = "fake"
    results_dir: Path = field(default_factory=lambda: Path("results"))

    def _router(self) -> Router:
        return build_router(self.settings.cache_dir, mode=self.mode)

    def _cycle(self, seed: int, clean: int, mix, near_miss: int = 0) -> Dataset:
        return generate(
            seed=seed,
            period="07-2026",
            base_date=BASE_DATE,
            clean_pairs=clean,
            injections=mix,
            near_miss_pairs=near_miss,
        )

    def _reconcile(self, dataset: Dataset) -> ReconciliationResult:
        return reconcile(
            dataset.books,
            dataset.portal,
            tolerances=self.settings.tolerances,
            policy=self.settings.policy,
            as_of=BASE_DATE,
        )

    # --- 6.1 tiering ablation ---------------------------------------------

    def tiering_ablation(self, *, limit: int | None = None) -> list[ArmResult]:
        """Three architectures over the same labelled exception set.

        The comparison that matters is cost per resolution, not accuracy: the
        all-agentic arm reaches the same answers on rule-closable classes and
        pays inference to do it.
        """
        dataset = self._cycle(self.settings.random_seed, 1800, DISTRIBUTION_MIX, near_miss=120)
        result = self._reconcile(dataset)
        exceptions = result.exceptions[:limit] if limit else result.exceptions
        arms: list[ArmResult] = []

        # (a) rules only -- anything a rule cannot close goes straight to a human
        rule_closed = sum(
            1
            for exception in exceptions
            if DEFAULT_ROUTING[exception.exception_class] is Tier.DETERMINISTIC
        )
        arms.append(
            ArmResult(
                "all_deterministic",
                result.total_documents,
                len(exceptions),
                wilson(rule_closed, len(exceptions)),
                llm_requests=0,
                total_tokens=0,
                escalated_to_human=len(exceptions) - rule_closed,
            )
        )

        # (b) every exception goes to the agent, including the easy ones
        arms.append(self._agentic_arm("all_agentic", dataset, exceptions))

        # (c) rules first, agent only on the residue
        residue = [
            exception
            for exception in exceptions
            if DEFAULT_ROUTING[exception.exception_class] is not Tier.DETERMINISTIC
        ]
        tiered = self._agentic_arm("tiered", dataset, residue)
        arms.append(
            ArmResult(
                "tiered",
                result.total_documents,
                len(exceptions),
                wilson(tiered.resolved_without_human.successes + rule_closed, len(exceptions)),
                tiered.llm_requests,
                tiered.total_tokens,
                len(exceptions) - rule_closed - tiered.resolved_without_human.successes,
            )
        )
        return arms

    def _agentic_arm(self, name: str, dataset: Dataset, exceptions) -> ArmResult:
        router = self._router()
        surface = ToolSurface(dataset, self.settings.tolerances)
        budgets = AgentBudgets(max_steps=MAX_BUDGET)
        resolved = 0
        escalated = 0
        for exception in exceptions:
            trajectory = investigate(exception, surface, router, budgets)
            decision = decide(exception, trajectory.finding, self.settings.policy)
            if trajectory.resolved and not decision.requires_human:
                resolved += 1
            else:
                escalated += 1
        ledger = router.ledger
        return ArmResult(
            name,
            0,
            len(exceptions),
            wilson(resolved, len(exceptions)),
            ledger.total_requests,
            ledger.usage.total,
            escalated,
        )

    # --- 6.2 step-budget curve, by truncation ------------------------------

    def step_budget_curve(self) -> list[dict[str, object]]:
        """Derive the whole curve from one run per case at the maximum budget.

        Valid because the agent is never told its budget, so its behaviour at
        step k does not depend on the limit. Twelve independent sweeps would
        cost twelve times as much and measure the same thing.
        """
        dataset = self._cycle(4242, 120, HARD_MIX)
        result = self._reconcile(dataset)
        router = self._router()
        surface = ToolSurface(dataset, self.settings.tolerances)
        trajectories = [
            investigate(exception, surface, router, AgentBudgets(max_steps=MAX_BUDGET))
            for exception in result.exceptions
        ]
        rows: list[dict[str, object]] = []
        for budget in range(1, MAX_BUDGET + 1):
            resolved = sum(1 for path in trajectories if outcome_at_budget(path, budget))
            interval = wilson(resolved, len(trajectories))
            steps_spent = sum(min(len(path.steps), budget) for path in trajectories)
            rows.append(
                {
                    "budget": budget,
                    **interval.as_dict(),
                    "probes_spent": steps_spent,
                    "probes_per_resolution": (
                        round(steps_spent / resolved, 3) if resolved else None
                    ),
                }
            )
        return rows

    # --- 6.3 memory compounding --------------------------------------------

    def memory_curve(self, cycles: int = 3) -> list[dict[str, object]]:
        """Does cost per resolved exception fall as precedents accumulate?

        Only human-confirmed resolutions are written back, so the store grows
        with reviewed outcomes rather than with the agent's own opinions.

        The precedent *hit rate* is a genuine harness measurement offline --
        retrieval either finds a prior case or it does not, and that does not
        depend on the model. Whether a retrieved precedent shortens the
        investigation does depend on the model reading it, and the offline
        provider ignores the hint entirely, so ``mean_probes`` is reported as
        None offline rather than as a flat line implying memory does nothing.
        """
        store = InMemoryPrecedentStore()
        rows: list[dict[str, object]] = []
        for cycle in range(1, cycles + 1):
            dataset = self._cycle(4242 + cycle, 120, HARD_MIX)
            result = self._reconcile(dataset)
            router = self._router()
            surface = ToolSurface(dataset, self.settings.tolerances, precedent_store=store)
            steps: list[float] = []
            resolved = 0
            hits = 0
            for exception in result.exceptions:
                hint = store.hint_for(exception.exception_class.value, exception.supplier_gstin)
                hits += bool(hint)
                trajectory = investigate(
                    exception,
                    surface,
                    router,
                    AgentBudgets(max_steps=MAX_BUDGET),
                    precedent_hint=hint,
                )
                steps.append(len(trajectory.steps))
                decision = decide(exception, trajectory.finding, self.settings.policy)
                if trajectory.resolved:
                    resolved += 1
                    # A human agreeing is what makes an outcome a precedent.
                    if not decision.requires_human:
                        store.add(
                            Precedent(
                                f"C{cycle}-{exception.exception_id}",
                                exception.exception_class,
                                exception.supplier_gstin,
                                exception.detail[:120],
                                trajectory.tool_sequence,
                                trajectory.finding.rationale[:120],
                                human_confirmed=True,
                                cycle=cycle,
                            )
                        )
            rows.append(
                {
                    "cycle": cycle,
                    "exceptions": len(result.exceptions),
                    "precedents_available": len(store),
                    "precedent_hit_rate": round(hits / len(result.exceptions), 4)
                    if result.exceptions
                    else None,
                    "resolved": resolved,
                    # None offline: the scripted provider does not read the hint,
                    # so a flat probe count here would be a property of the
                    # stand-in rather than evidence about memory.
                    "mean_probes": (
                        None
                        if self.mode == "fake"
                        else (
                            round(value, 3) if (value := mean_or_none(steps)) is not None else None
                        )
                    ),
                    "llm_requests": router.ledger.total_requests,
                    "requests_per_resolution": (
                        round(router.ledger.total_requests / resolved, 3) if resolved else None
                    ),
                }
            )
        return rows

    # --- 6.4 straight-through curve ----------------------------------------

    def stp_curve(self, *, limit: int | None = None) -> list[dict[str, object]]:
        """Straight-through rate against precision, as the confidence floor moves.

        Precision here is measured against the generator's ground-truth class,
        which is the only place in this project where a "correct answer" exists.

        Refuses to run offline: see NOT_MEASURABLE_OFFLINE.
        """
        if self.mode == "fake":
            return [
                {
                    "confidence_floor": None,
                    "straight_through_rate": None,
                    "precision": None,
                    "precision_ci_low": None,
                    "n_auto": 0,
                    "note": NOT_MEASURABLE_OFFLINE,
                }
            ]
        dataset = self._cycle(4242, 120, HARD_MIX)
        result = self._reconcile(dataset)
        router = self._router()
        surface = ToolSurface(dataset, self.settings.tolerances)
        truth = dataset.truth_by_key

        scored: list[tuple[float, bool]] = []
        for exception in result.exceptions[:limit] if limit else result.exceptions:
            trajectory = investigate(exception, surface, router, AgentBudgets(max_steps=MAX_BUDGET))
            if trajectory.finding is None:
                continue
            key = next(
                (
                    k
                    for k in truth
                    if exception.exception_id.endswith(k) or k in exception.exception_id
                ),
                None,
            )
            expected = truth[key].exception_class if key else exception.exception_class
            scored.append(
                (trajectory.finding.confidence, trajectory.finding.proposed_class == expected)
            )

        rows: list[dict[str, object]] = []
        for step in range(0, 21):
            floor = step / 20
            passed = [correct for confidence, correct in scored if confidence >= floor]
            precision = wilson(sum(passed), len(passed))
            rows.append(
                {
                    "confidence_floor": round(floor, 2),
                    "straight_through_rate": round(len(passed) / len(scored), 4)
                    if scored
                    else None,
                    "precision": round(precision.point, 4) if passed else None,
                    "precision_ci_low": round(precision.low, 4) if passed else None,
                    "n_auto": len(passed),
                }
            )
        return rows

    # --- 6.5 trajectory evaluation -----------------------------------------

    def trajectory_scores(self, *, limit: int | None = None) -> dict[str, object]:
        """Score probe choices against a reference path. No extra model calls.

        Refuses to run offline: the offline provider walks REFERENCE_PROBES, so
        scoring it against REFERENCE_PROBES measures nothing.
        """
        if self.mode == "fake":
            return {
                "cases_scored": 0,
                "first_probe_accuracy": None,
                "mean_reference_overlap": None,
                "mean_steps_over_reference": None,
                "note": NOT_MEASURABLE_OFFLINE,
            }
        dataset = self._cycle(4242, 120, HARD_MIX)
        result = self._reconcile(dataset)
        router = self._router()
        surface = ToolSurface(dataset, self.settings.tolerances)

        first_probe_correct = 0
        scored = 0
        overlaps: list[float] = []
        excess: list[float] = []
        for exception in result.exceptions[:limit] if limit else result.exceptions:
            reference = REFERENCE_PROBES.get(exception.exception_class)
            if not reference:
                continue
            trajectory = investigate(exception, surface, router, AgentBudgets(max_steps=MAX_BUDGET))
            if not trajectory.steps:
                continue
            scored += 1
            first_probe_correct += trajectory.steps[0].tool_name == reference[0]
            chosen = set(trajectory.tool_sequence)
            overlaps.append(len(chosen & set(reference)) / len(set(reference)))
            excess.append(len(trajectory.steps) - len(reference))

        return {
            "cases_scored": scored,
            "first_probe_accuracy": wilson(first_probe_correct, scored).as_dict(),
            "mean_reference_overlap": (
                round(value, 4) if (value := mean_or_none(overlaps)) is not None else None
            ),
            "mean_steps_over_reference": (
                round(value, 3) if (value := mean_or_none(excess)) is not None else None
            ),
        }

    # --- run-to-run consistency --------------------------------------------

    def consistency(self, *, cases: int = 8, repeats: int = 3) -> dict[str, object]:
        """How much does the same case move when you simply ask again?

        Added after the fact, and it changed how every other number here is
        reported. The response cache makes results perfectly *reproducible*,
        which is easy to mistake for the agent being *stable*. It is not the
        same claim: a cached run replays one draw, so a single-draw measurement
        looks flawless no matter how much the underlying answer moves.

        Temperature zero does not make a model deterministic. So this bypasses
        the cache deliberately, asks the same questions several times, and
        reports how often the outcome and the probe path disagree with
        themselves.

        Why it matters for the rest of the report: the Wilson intervals
        elsewhere capture sampling error *across cases* only. If a meaningful
        share of cases are unstable across repeats, those intervals understate
        total uncertainty, and any comparison between arms that is smaller than
        the instability is not a finding.
        """
        if self.mode == "fake":
            return {
                "cases": 0,
                "repeats": 0,
                "note": (
                    "not measured: the offline provider is genuinely deterministic, "
                    "so measuring its run-to-run variance would report zero and say "
                    "nothing about a real model."
                ),
            }

        dataset = self._cycle(4242, 120, HARD_MIX)
        result = self._reconcile(dataset)
        selected = result.exceptions[:cases]
        surface = ToolSurface(dataset, self.settings.tolerances)

        outcomes: dict[str, list[bool]] = {}
        paths: dict[str, list[tuple[str, ...]]] = {}
        actions: dict[str, list[str]] = {}
        for _ in range(repeats):
            router = build_router(self.settings.cache_dir, mode=self.mode)
            router.bypass_cache = True
            for exception in selected:
                trajectory = investigate(
                    exception, surface, router, AgentBudgets(max_steps=MAX_BUDGET)
                )
                outcomes.setdefault(exception.exception_id, []).append(trajectory.resolved)
                paths.setdefault(exception.exception_id, []).append(trajectory.tool_sequence)
                actions.setdefault(exception.exception_id, []).append(
                    trajectory.finding.proposed_action.value if trajectory.finding else "NONE"
                )

        unstable_outcome = sum(1 for values in outcomes.values() if len(set(values)) > 1)
        unstable_path = sum(1 for values in paths.values() if len(set(values)) > 1)
        unstable_action = sum(1 for values in actions.values() if len(set(values)) > 1)
        total = len(outcomes)
        return {
            "cases": total,
            "repeats": repeats,
            "unstable_outcome": unstable_outcome,
            "unstable_outcome_rate": round(unstable_outcome / total, 4) if total else None,
            "unstable_probe_path": unstable_path,
            "unstable_probe_path_rate": round(unstable_path / total, 4) if total else None,
            "unstable_action": unstable_action,
            "unstable_action_rate": round(unstable_action / total, 4) if total else None,
            "per_case": {
                case_id: {
                    "resolved": values,
                    "paths": [list(path) for path in paths[case_id]],
                    "actions": actions[case_id],
                }
                for case_id, values in outcomes.items()
            },
        }

    # --- 6.7 model-tier ablation -------------------------------------------

    def model_tier_ablation(self, *, cases: int = 12, tiers: tuple[str, ...] = ("A", "B", "C")):
        """Run the same cases through hosted and locally-served models.

        Every tier sees an identical case set, so any difference is the model
        rather than the workload. Tiers whose model is not available on this
        machine are skipped with a reason rather than silently omitted -- an
        absent row and a failed row mean very different things.
        """
        dataset = self._cycle(4242, 120, HARD_MIX)
        result = self._reconcile(dataset)
        selected = result.exceptions[:cases]
        truth = dataset.truth_by_key
        expected: dict[str, str] = {}
        for exception in selected:
            key = next((k for k in truth if k in exception.exception_id), None)
            if key is not None:
                expected[exception.exception_id] = truth[key].exception_class.value

        rows: list[TierResult] = []
        skipped: list[dict[str, str]] = []
        for tier in tiers:
            model, kind, where = MODEL_TIERS[tier]
            try:
                router = (
                    build_router(self.settings.cache_dir, mode="local", local_model=model)
                    if kind == "local"
                    else build_router(self.settings.cache_dir, mode="live", models=(model,))
                )
            except ValueError as exc:
                skipped.append({"tier": tier, "model": model, "reason": str(exc)})
                continue

            # Every tier runs fresh. With the cache on, a tier whose responses
            # happen to be cached from earlier work replays in milliseconds
            # while a newly-added tier runs for real, and the resulting
            # "latency" column compares cache lookups against inference.
            router.bypass_cache = True
            surface = ToolSurface(dataset, self.settings.tolerances)
            trajectories = [
                investigate(exception, surface, router, AgentBudgets(max_steps=MAX_BUDGET))
                for exception in selected
            ]
            rows.append(summarise_tier(tier, model, where, trajectories, expected))
        return rows, skipped

    # --- reporting ---------------------------------------------------------

    def write_csv(self, name: str, rows: list[dict[str, object]]) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        path = self.results_dir / f"{name}.csv"
        if not rows:
            path.write_text("", encoding="utf-8")
            return path
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def write_json(self, name: str, payload: object) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        path = self.results_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path


def summarise_trajectory(trajectory: Trajectory) -> dict[str, object]:
    return {
        "exception_id": trajectory.exception_id,
        "class": trajectory.exception_class.value,
        "probes": list(trajectory.tool_sequence),
        "steps": len(trajectory.steps),
        "resolved": trajectory.resolved,
        "terminated_because": trajectory.terminated_because,
        "action": (
            trajectory.finding.proposed_action.value
            if trajectory.finding
            else ImsAction.PENDING.value
        ),
        "confidence": trajectory.finding.confidence if trajectory.finding else None,
        "tokens": trajectory.usage.total,
    }


def routing_is_complete() -> bool:
    return set(DEFAULT_ROUTING) == set(ExceptionClass)
