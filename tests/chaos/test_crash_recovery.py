"""Kill the process mid-submit and prove the portal was acted on exactly once.

Marked ``chaos`` and skipped unless Postgres is reachable, so the default suite
stays offline and dependency-free. These are the only tests in the project that
need a database, because they are the only ones asserting something about what
survives a process death.

What is being demonstrated, in one sentence: completed steps are memoised and
do not run again, the interrupted step *does* run again, and the derived
idempotency key is what makes that second run harmless.

The last clause is the point. Durable execution alone does not give exactly-once
side effects -- it gives at-least-once for the step that was interrupted. The
exactly-once property is a joint result of the engine's memoisation and a key
the retry can reproduce.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO = Path(__file__).resolve().parents[2]
WORKER = REPO / "tests" / "chaos" / "worker.py"
URL = "postgresql://gstrecon:gstrecon@localhost:5433/gstrecon"
DOCUMENTS = 6
CRASH_AT = 3


def _engine():
    return sa.create_engine(URL.replace("postgresql://", "postgresql+psycopg://"))


def _postgres_reachable() -> bool:
    try:
        engine = _engine()
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.chaos,
    pytest.mark.skipif(
        not _postgres_reachable(),
        reason="chaos tests need Postgres on localhost:5433 (docker compose up -d)",
    ),
]


def _run(args: list[str], crash_at: int) -> subprocess.CompletedProcess:
    environment = {**os.environ, "CRASH_AT": str(crash_at)}
    return subprocess.run(
        [sys.executable, str(WORKER), *args],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _query(sql: str):
    with _engine().begin() as connection:
        return connection.execute(sa.text(sql)).fetchall()


@pytest.fixture
def clean_portal():
    with _engine().begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS chaos_portal, chaos_attempts"))
    yield


@pytest.fixture
def crashed_then_resumed(clean_portal):
    """Run a cycle that dies mid-submit, then resume it in a fresh process."""
    workflow_id = f"chaos-{uuid.uuid4().hex[:12]}"
    crashed = _run([workflow_id, str(DOCUMENTS), "start"], crash_at=CRASH_AT)
    assert crashed.returncode == 9, f"worker did not die as instructed: {crashed.stderr[-400:]}"

    time.sleep(0.5)  # let the engine's own write settle before a second process reads it
    resumed = _run([workflow_id, str(DOCUMENTS), "recover"], crash_at=-1)
    assert resumed.returncode == 0, resumed.stderr[-600:]
    return resumed


def test_every_document_is_acted_on_exactly_once(crashed_then_resumed) -> None:
    """The headline safety number: zero documents actioned twice.

    A reject purges the invoice value from GSTR-2B for the period and cannot be
    undone within the cycle, so a duplicate submission is not a harmless repeat.
    """
    duplicated = _query(
        "SELECT document_id FROM chaos_portal GROUP BY document_id HAVING count(*) > 1"
    )
    assert duplicated == []

    rows = _query("SELECT count(*), count(DISTINCT document_id) FROM chaos_portal")
    assert rows[0][0] == DOCUMENTS
    assert rows[0][1] == DOCUMENTS


def test_completed_steps_are_not_re_executed(crashed_then_resumed) -> None:
    """Steps finished before the crash must not run a second time.

    One retry, not four: attempts should be one more than the document count,
    accounting for the interrupted step alone.
    """
    attempts = _query("SELECT count(*) FROM chaos_attempts")[0][0]
    assert attempts == DOCUMENTS + 1


def test_the_interrupted_step_does_re_run_and_is_recognised(crashed_then_resumed) -> None:
    """At-least-once for the interrupted step, made safe by the derived key.

    The document the process died on is submitted twice. The second submission
    presents the same key and is answered ALREADY_APPLIED rather than acting
    again -- which is only possible because the key is derived from the
    decision rather than generated per attempt.
    """
    counts = dict(_query("SELECT document_id, submissions_seen FROM chaos_portal"))
    assert counts[f"DOC-{CRASH_AT}"] == 2, "the interrupted step should have been retried"
    for index in range(DOCUMENTS):
        if index != CRASH_AT:
            assert counts[f"DOC-{index}"] == 1


def test_resumed_run_reports_the_replay_rather_than_a_fresh_action(
    crashed_then_resumed,
) -> None:
    assert "ALREADY_APPLIED" in crashed_then_resumed.stdout
    assert crashed_then_resumed.stdout.count("ALREADY_APPLIED") == 1
