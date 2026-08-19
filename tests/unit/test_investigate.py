"""Tier 2 investigation agent tests, all offline.

The interesting cases here are the ones where the agent misbehaves. A bounded
agent is only bounded if the bounds actually fire, so each guard gets a test
that provokes it deliberately rather than hoping a real model will.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from gst_recon.agents.investigate import (
    SYSTEM_PROMPT,
    investigate,
    outcome_at_budget,
)
from gst_recon.agents.tools import ToolSurface
from gst_recon.config import AgentBudgets, MatchTolerances, PolicyThresholds
from gst_recon.data.generator import generate
from gst_recon.domain.taxonomy import ExceptionClass
from gst_recon.llm import FakeProvider, Router, Shard
from gst_recon.matching.engine import reconcile

BASE_DATE = date(2026, 7, 14)
TOLERANCES = MatchTolerances()
POLICY = PolicyThresholds()


def _fixture(
    cls: ExceptionClass,
    tmp_path: Path,
    *,
    failure_mode: str | None = None,
    budgets: AgentBudgets | None = None,
):
    dataset = generate(
        seed=31, period="07-2026", base_date=BASE_DATE, clean_pairs=40, injections={cls: 4}
    )
    result = reconcile(
        dataset.books, dataset.portal, tolerances=TOLERANCES, policy=POLICY, as_of=BASE_DATE
    )
    exception = next(e for e in result.exceptions if e.exception_class is cls)
    surface = ToolSurface(dataset, TOLERANCES)
    router = Router(
        [Shard(FakeProvider(failure_mode=failure_mode), requests_per_minute=10**6)], tmp_path
    )
    return exception, surface, router, budgets or AgentBudgets()


def test_agent_resolves_a_missing_in_books_case(tmp_path: Path) -> None:
    exception, surface, router, budgets = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
    trajectory = investigate(exception, surface, router, budgets)
    assert trajectory.resolved
    assert trajectory.terminated_because == "completed"
    assert trajectory.finding is not None
    assert trajectory.finding.evidence


def test_agent_probes_adaptively_rather_than_prefetching(tmp_path: Path) -> None:
    """The whole case for an agent here is that it stops when it has enough."""
    exception, surface, router, budgets = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
    trajectory = investigate(exception, surface, router, budgets)
    assert 0 < len(trajectory.steps) < len(surface.specs)


def test_evidence_must_cite_a_real_tool_call(tmp_path: Path) -> None:
    """A fluent rationale over invented provenance is the worst possible output.

    It reads exactly like a good answer, so it has to be rejected structurally
    rather than spotted by a reviewer.
    """
    exception, surface, router, budgets = _fixture(
        ExceptionClass.MISSING_IN_BOOKS, tmp_path, failure_mode="no_evidence"
    )
    trajectory = investigate(exception, surface, router, budgets)
    assert trajectory.finding is None or not trajectory.finding.evidence
    assert not trajectory.resolved


def test_loop_detection_stops_a_repeating_agent(tmp_path: Path) -> None:
    exception, surface, router, budgets = _fixture(
        ExceptionClass.MISSING_IN_BOOKS, tmp_path, failure_mode="loop"
    )
    trajectory = investigate(exception, surface, router, budgets)
    assert trajectory.terminated_because == "loop_detected"
    assert len(trajectory.steps) < budgets.max_steps


def test_step_budget_is_enforced_by_the_harness(tmp_path: Path) -> None:
    exception, surface, router, budgets = _fixture(
        ExceptionClass.MISSING_IN_BOOKS,
        tmp_path,
        failure_mode="loop",
        budgets=AgentBudgets(max_steps=3, loop_repeat_threshold=10**6),
    )
    trajectory = investigate(exception, surface, router, budgets)
    assert trajectory.terminated_because == "step_budget_exhausted"
    assert len(trajectory.steps) == 3


def test_prompt_never_reveals_the_step_budget() -> None:
    """The truncation trick depends on this, so it is asserted, not assumed.

    If the agent learned its budget, behaviour at step k would depend on the
    limit and a long run would no longer contain the short-run outcomes.
    """
    lowered = SYSTEM_PROMPT.lower()
    for leak in ("budget", "max_steps", "steps remaining", "you have 8", "limit of"):
        assert leak not in lowered


def test_budget_curve_is_derivable_from_one_trajectory(tmp_path: Path) -> None:
    exception, surface, router, budgets = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
    trajectory = investigate(exception, surface, router, budgets)
    used = len(trajectory.steps)

    assert not outcome_at_budget(trajectory, used - 1)
    assert outcome_at_budget(trajectory, used)
    assert outcome_at_budget(trajectory, used + 5)


def test_agent_may_conclude_after_spending_its_last_probe(tmp_path: Path) -> None:
    """Regression: spending the whole budget on probes discarded the answer.

    The budget was checked at the top of the loop, so an investigation that
    needed exactly ``max_steps`` probes ran all of them and was then cut off
    before the model was asked for a verdict. Every probe it had just run was
    thrown away and the case reported as unresolved -- and because the derived
    step-budget curve disagreed with a real run at that budget, the whole
    truncation argument was invalid.

    A budget of N means N probes are allowed, not that stating a conclusion
    counts against it.
    """
    exception, surface, router, _ = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
    full = investigate(exception, surface, router, AgentBudgets(max_steps=8))
    exactly = len(full.steps)

    exception2, surface2, router2, _ = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
    trimmed = investigate(exception2, surface2, router2, AgentBudgets(max_steps=exactly))
    assert trimmed.resolved
    assert trimmed.terminated_because == "completed"
    assert len(trimmed.steps) == exactly


def test_truncation_matches_an_actual_shorter_run(tmp_path: Path) -> None:
    """The derived curve must agree with really running at that budget."""
    exception, surface, router, _ = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
    full = investigate(exception, surface, router, AgentBudgets(max_steps=8))
    needed = len(full.steps)

    for budget in range(1, needed + 2):
        exception2, surface2, router2, _ = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
        actual = investigate(exception2, surface2, router2, AgentBudgets(max_steps=budget))
        assert actual.resolved == outcome_at_budget(full, budget), (
            f"budget {budget}: real run resolved={actual.resolved}, "
            f"truncation predicted {outcome_at_budget(full, budget)}"
        )


@pytest.mark.parametrize(
    "exception_class",
    [
        ExceptionClass.MISSING_IN_BOOKS,
        ExceptionClass.AMOUNT_MISMATCH,
        ExceptionClass.GSTIN_MISMATCH,
        ExceptionClass.CREDIT_NOTE_UNLINKED,
    ],
)
def test_every_tier_two_class_produces_a_trajectory(
    exception_class: ExceptionClass, tmp_path: Path
) -> None:
    exception, surface, router, budgets = _fixture(exception_class, tmp_path)
    trajectory = investigate(exception, surface, router, budgets)
    assert trajectory.steps
    assert trajectory.exception_class is exception_class


def test_tool_surface_offers_nothing_that_mutates(tmp_path: Path) -> None:
    """The read-only guarantee is structural: no mutating tool exists to call."""
    _, surface, _, _ = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
    forbidden = ("submit", "accept", "reject", "write", "update", "delete", "send", "create")
    for spec in surface.specs:
        assert not any(word in spec.name.lower() for word in forbidden)


def test_unknown_tool_is_reported_not_raised(tmp_path: Path) -> None:
    _, surface, _, _ = _fixture(ExceptionClass.MISSING_IN_BOOKS, tmp_path)
    result = surface.invoke("tc-x", "drop_database", {})
    assert "error" in result.payload
    assert "available" in result.payload
