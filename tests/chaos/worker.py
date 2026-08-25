"""A durable submit run that can be killed at a chosen step.

Run as a subprocess by the chaos tests. The portal it submits to is backed by
Postgres rather than held in memory, because the whole question is what
survives the process dying -- an in-process portal would die with it and the
test would prove nothing.

Usage:
    worker.py <workflow-id> <n-documents> [crash-at-step]
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID

from gst_recon.domain.taxonomy import ImsAction
from gst_recon.gstn import ImsSubmission, SubmitOutcome, SubmitResult
from gst_recon.workflow.durable import DurableConfig

URL = "postgresql://gstrecon:gstrecon@localhost:5433/gstrecon"
ENGINE = sa.create_engine(URL.replace("postgresql://", "postgresql+psycopg://"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS chaos_portal (
    idempotency_key TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    action          TEXT NOT NULL,
    submissions_seen   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS chaos_attempts (
    id           SERIAL PRIMARY KEY,
    document_id  TEXT NOT NULL
);
"""


def ensure_schema() -> None:
    with ENGINE.begin() as connection:
        connection.execute(sa.text(SCHEMA))


class DurablePortal:
    """A portal whose applied-actions ledger lives in Postgres.

    One row per applied action, so the row count is the exactly-once evidence.
    ``submissions_seen`` counts how many times a key was *presented*, which is
    not the same as how many times the action was applied -- a replay bumps the
    counter and changes nothing else.

    It is counted rather than guarded on purpose. If the idempotency check were
    left to the primary key, a double submit would raise and the test would
    pass for the wrong reason. Counting means a duplicate is recorded and the
    assertion can see it.
    """

    def submit_ims_action(self, submission: ImsSubmission) -> SubmitResult:
        key = submission.idempotency_key()
        with ENGINE.begin() as connection:
            # Every attempt is logged, applied or not, so the test can tell a
            # step that re-ran from one that never ran.
            connection.execute(
                sa.text("INSERT INTO chaos_attempts(document_id) VALUES (:d)"),
                {"d": submission.document_id},
            )
            existing = connection.execute(
                sa.text("SELECT submissions_seen FROM chaos_portal WHERE idempotency_key = :k"),
                {"k": key},
            ).fetchone()
            if existing is not None:
                connection.execute(
                    sa.text(
                        "UPDATE chaos_portal SET submissions_seen = submissions_seen + 1 "
                        "WHERE idempotency_key = :k"
                    ),
                    {"k": key},
                )
                return SubmitResult(
                    SubmitOutcome.ALREADY_APPLIED, key, f"REF-{key[:12]}", "replayed"
                )
            connection.execute(
                sa.text(
                    "INSERT INTO chaos_portal(idempotency_key, document_id, action) "
                    "VALUES (:k, :d, :a)"
                ),
                {"k": key, "d": submission.document_id, "a": submission.action.value},
            )
        return SubmitResult(SubmitOutcome.APPLIED, key, f"REF-{key[:12]}", "applied")


config = DurableConfig(database_url=URL, application_version="chaos-v1")
DBOS(config=config.as_dbos_config())
PORTAL = DurablePortal()
CRASH_AT = int(os.environ.get("CRASH_AT", "-1"))


@DBOS.step()
def submit_document(index: int, document_id: str) -> str:
    """One submission. The only step here with an external consequence."""
    if index == CRASH_AT:
        # Kill after the portal has applied the action but before the step
        # completes, which is the interleaving idempotency exists for.
        PORTAL.submit_ims_action(_submission(document_id))
        os._exit(9)
    return PORTAL.submit_ims_action(_submission(document_id)).outcome.value


def _submission(document_id: str) -> ImsSubmission:
    return ImsSubmission(
        taxpayer_gstin="27AAPFU0939F1ZV",
        return_period="07-2026",
        document_id=document_id,
        action=ImsAction.REJECT,
        ruleset_version="2026.07",
    )


@DBOS.workflow()
def submit_cycle(count: int) -> list[str]:
    return [submit_document(index, f"DOC-{index}") for index in range(count)]


def main() -> int:
    ensure_schema()
    DBOS.launch()
    workflow_id, count = sys.argv[1], int(sys.argv[2])
    if sys.argv[3] == "start":
        with SetWorkflowID(workflow_id):
            print("result:", submit_cycle(count))
    else:
        pending = DBOS.list_workflows(status=["PENDING", "ENQUEUED"])
        print("recoverable:", [status.workflow_id for status in pending])
        for status in pending:
            print("resumed:", DBOS.resume_workflow(status.workflow_id).get_result())
    DBOS.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
