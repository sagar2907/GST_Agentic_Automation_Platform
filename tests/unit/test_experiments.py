"""Statistics and the harness's refusal to report circular metrics.

The tests that matter most here are the ones asserting that certain numbers
are *not* produced. A project that invents its own numbers is worse than one
with fewer numbers, so "not measured" has to be a pinned behaviour rather than
a habit of whoever is writing the report that day.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gst_recon.config import load_settings
from gst_recon.experiments.harness import NOT_MEASURABLE_OFFLINE, Experiments
from gst_recon.experiments.stats import (
    difference_is_significant,
    mean_or_none,
    wilson,
)

# --- interval estimation ---------------------------------------------------


def test_perfect_score_does_not_claim_certainty() -> None:
    """18 of 18 is not proof of 100%, and the interval must say so.

    The normal approximation returns [1.0, 1.0] here -- eighteen observations
    presented as certainty. Wilson keeps a lower bound below 1 and inside the
    unit range, which is the whole reason this project does not use the
    textbook formula.
    """
    interval = wilson(18, 18)
    assert interval.point == 1.0
    assert interval.low < 1.0
    assert interval.high == 1.0


def test_zero_score_stays_inside_the_unit_range() -> None:
    interval = wilson(0, 30)
    assert interval.low == 0.0
    assert 0.0 < interval.high < 1.0


def test_interval_narrows_as_the_sample_grows() -> None:
    assert wilson(35, 50).half_width > wilson(350, 500).half_width


def test_no_trials_is_not_measured_rather_than_zero_percent() -> None:
    interval = wilson(0, 0)
    assert interval.trials == 0
    assert "not measured" in interval.render()


def test_render_carries_the_sample_size() -> None:
    assert "n=60" in wilson(42, 60).render()


def test_mean_of_nothing_is_none_not_zero() -> None:
    """A zero would be printed as a measurement; None forces an honest label."""
    assert mean_or_none([]) is None
    assert mean_or_none([1.0, 2.0]) == 1.5


def test_significance_requires_non_overlapping_intervals() -> None:
    assert difference_is_significant(wilson(5, 100), wilson(80, 100))
    assert not difference_is_significant(wilson(50, 100), wilson(55, 100))
    assert not difference_is_significant(wilson(1, 1), wilson(0, 0))


# --- the harness refuses circular measurements -----------------------------


@pytest.fixture
def offline(tmp_path: Path) -> Experiments:
    settings = load_settings()
    settings.cache_dir = tmp_path / "cache"
    return Experiments(settings, mode="fake", results_dir=tmp_path / "results")


def test_trajectory_scoring_refuses_to_run_offline(offline: Experiments) -> None:
    """Regression: offline scoring reported flawless trajectories.

    The offline provider walks REFERENCE_PROBES, and the trajectory evaluator
    scores against REFERENCE_PROBES. Run together they produced first-probe
    accuracy 1.000, reference overlap 1.000 and zero excess steps -- the
    evaluator marking a script against its own answer key. Those figures were
    the most flattering in the project and meant nothing.
    """
    scores = offline.trajectory_scores()
    assert scores["first_probe_accuracy"] is None
    assert scores["mean_reference_overlap"] is None
    assert scores["cases_scored"] == 0
    assert scores["note"] == NOT_MEASURABLE_OFFLINE


def test_stp_curve_refuses_to_run_offline(offline: Experiments) -> None:
    """The scripted provider reads the true class from the prompt.

    Precision was therefore 1.000 at every confidence threshold, which says
    nothing about a model and everything about the stand-in.
    """
    rows = offline.stp_curve()
    assert len(rows) == 1
    assert rows[0]["precision"] is None
    assert rows[0]["note"] == NOT_MEASURABLE_OFFLINE


def test_memory_curve_reports_hit_rate_but_not_probe_savings(offline: Experiments) -> None:
    """Retrieval is a harness property; acting on a hint is a model property.

    Whether a precedent is found does not depend on the model, so hit rate is
    honestly measurable offline. Whether finding one shortens the work does
    depend on the model reading it, and the offline provider ignores the hint.
    """
    rows = offline.memory_curve(cycles=2)
    assert all(row["mean_probes"] is None for row in rows)
    assert all(row["precedent_hit_rate"] is not None for row in rows)
    assert rows[-1]["precedents_available"] > rows[0]["precedents_available"]


def test_cost_metrics_are_still_measured_offline(offline: Experiments) -> None:
    """Request counts are a property of the harness, not of the model."""
    arms = offline.tiering_ablation(limit=40)
    by_name = {arm.name: arm for arm in arms}
    assert by_name["all_deterministic"].llm_requests == 0
    assert by_name["all_agentic"].llm_requests > 0
    assert by_name["tiered"].llm_requests <= by_name["all_agentic"].llm_requests


def test_step_budget_curve_is_monotonic(offline: Experiments) -> None:
    """More budget can never resolve fewer cases."""
    rows = offline.step_budget_curve()
    resolved = [row["successes"] for row in rows]
    assert resolved == sorted(resolved)
