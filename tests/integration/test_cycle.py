"""The whole cycle, end to end, offline.

Reconcile, route, investigate, gate, record, submit. No database, no model, no
portal -- a fake at each boundary, so this runs from a clean clone with no
credentials and still exercises the real orchestration.

The assertions here are about the properties the cycle must hold whatever the
data happens to be, not about particular counts, because counts move whenever
the generator's mix is retuned and a test that pins them would fail for reasons
nobody cares about.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from gst_recon.config import load_settings
from gst_recon.data.generator import HARD_MIX, generate
from gst_recon.domain.taxonomy import ImsAction
from gst_recon.gstn import FakeGspClient
from gst_recon.llm import build_router
from gst_recon.workflow import SUBMITTABLE, run_cycle

AS_OF = date(2026, 7, 10)
AT = datetime(2026, 7, 10, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def cycle(tmp_path):
    settings = load_settings()
    settings.cache_dir = tmp_path / "cache"
    dataset = generate(
        seed=4242,
        period="07-2026",
        base_date=date(2026, 7, 14),
        clean_pairs=120,
        injections=HARD_MIX,
    )
    client = FakeGspClient()
    router = build_router(settings.cache_dir, mode="fake")
    outcome = run_cycle(dataset, client, router, settings, as_of=AS_OF, recorded_at=AT, limit=20)
    return outcome, client


def test_cycle_completes_and_accounts_for_every_exception(cycle) -> None:
    outcome, _ = cycle
    assert outcome.exceptions == 20
    assert len(outcome.decisions) == outcome.exceptions
    assert len(outcome.audit) == outcome.exceptions


def test_every_decision_is_recorded_before_it_could_be_acted_on(cycle) -> None:
    """One audit entry per decision, and the chain intact.

    A decision that reached the portal without reaching the log would be an
    action nobody can explain afterwards.
    """
    outcome, _ = cycle
    assert outcome.audit.verify_chain() == (True, "chain intact")
    recorded = {entry.exception_id for entry in outcome.audit.entries}
    assert recorded == {decision.exception_id for decision in outcome.decisions}


def test_nothing_requiring_a_human_was_submitted(cycle) -> None:
    """The gate is the control, so it must actually gate.

    Measured elsewhere in this project: the same case can be proposed ACCEPT on
    one run and REJECT on the next. That makes this assertion the load-bearing
    one in the file.
    """
    outcome, client = cycle
    gated = {d.exception_id for d in outcome.decisions if d.requires_human}
    submitted = set(client.portal_state)
    assert gated & submitted == set()


def test_only_submittable_actions_reach_the_portal(cycle) -> None:
    """PENDING is the absence of a decision and must never be sent."""
    _outcome, client = cycle
    for action in client.portal_state.values():
        assert ImsAction(action) in SUBMITTABLE


def test_no_exactly_once_violations_on_a_clean_run(cycle) -> None:
    outcome, _ = cycle
    assert outcome.exactly_once_violations == 0


def test_rerunning_the_cycle_submits_nothing_new(tmp_path) -> None:
    """The whole cycle is replay-safe, not just the submit call.

    Re-running against the same portal must produce replays rather than a
    second set of actions -- the property a scheduler retrying a failed cycle
    depends on.
    """
    settings = load_settings()
    settings.cache_dir = tmp_path / "cache"
    dataset = generate(
        seed=4242,
        period="07-2026",
        base_date=date(2026, 7, 14),
        clean_pairs=120,
        injections=HARD_MIX,
    )
    client = FakeGspClient()
    router = build_router(settings.cache_dir, mode="fake")

    first = run_cycle(dataset, client, router, settings, as_of=AS_OF, recorded_at=AT, limit=20)
    calls_after_first = client.calls
    second = run_cycle(dataset, client, router, settings, as_of=AS_OF, recorded_at=AT, limit=20)

    assert second.submitted == 0
    assert second.replayed == first.submitted
    assert client.calls == calls_after_first
    assert second.exactly_once_violations == 0


def test_cut_off_distance_is_computed_from_the_supplied_date(cycle) -> None:
    """Cut-off behaviour is only testable against a clock you control."""
    outcome, _ = cycle
    assert outcome.days_to_cutoff == 4
