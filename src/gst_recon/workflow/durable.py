"""Durable execution around the cycle.

The pipeline in ``workflow.pipeline`` is ordinary code. This module makes it
survive the process dying halfway through, which matters here more than in most
systems: an inward document left unactioned at the cut-off is *treated as
accepted*, so a crashed run does not fail safe. It silently accepts whatever it
had not reached yet.

The durability contract, measured against this engine rather than assumed:

* A step that completed before the crash is memoised and **does not run again**.
* The step that was **interrupted runs again** on resume.
* After a hard kill the workflow's status is ``ENQUEUED``, not ``PENDING``, so
  recovery has to look for both.
* The engine keeps its ledger in a **separate system database** (``*_dbos_sys``),
  not in the application schema -- resetting state means resetting that.

The second point is the whole reason ``gstn.ImsSubmission`` derives its
idempotency key from the decision content. Resume re-runs the interrupted step,
so the submit call happens twice; deriving the key means the second call
presents the same key the first did and the portal replays its original outcome
instead of applying the action again.

DBOS owns durability. The agent loop inside a step is a step, not a second
checkpointing layer -- letting both persist the same progress is how that
boundary goes wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from gst_recon.gstn import ImsSubmission, SubmitResult

DEFAULT_APP_NAME = "gst-recon"


@dataclass(frozen=True, slots=True)
class DurableConfig:
    database_url: str
    app_name: str = DEFAULT_APP_NAME
    application_version: str = "cycle-v1"

    def as_dbos_config(self) -> dict:
        return {
            "name": self.app_name,
            "database_url": self.database_url,
            "run_admin_server": False,
            "console_log_level": "WARNING",
            # Pinned rather than derived from source. The engine only recovers
            # workflows whose version matches, and an auto-derived version
            # changes whenever the code does -- which would silently orphan
            # every workflow suspended across a deploy, exactly the ones that
            # most need recovering.
            "application_version": self.application_version,
        }


# Statuses a workflow can hold after an abrupt termination. ENQUEUED is the one
# that is easy to miss: a hard kill leaves the workflow there rather than in
# PENDING, so a recovery sweep filtering on PENDING alone finds nothing and
# reports a clean slate.
RECOVERABLE_STATUSES = ("PENDING", "ENQUEUED")


def workflow_id_for(taxpayer_gstin: str, period: str, ruleset_version: str) -> str:
    """A stable id for one taxpayer's cycle.

    Stable so a re-invocation after a crash addresses the same workflow rather
    than starting a second one alongside it. The ruleset version is included
    because a cycle re-run under changed rules is a different piece of work and
    should not silently resume the old one's progress.
    """
    return f"cycle:{taxpayer_gstin}:{period}:{ruleset_version}"


def submit_step(client, submission: ImsSubmission) -> SubmitResult:
    """The one step whose re-execution has an external consequence.

    Isolated in its own function so the chaos tests can interrupt precisely
    here. Everything before it is read-only or local; this is the boundary
    where the system stops thinking and starts acting.
    """
    return client.submit_ims_action(submission)


def build_durable_cycle(config: DurableConfig):
    """Register the cycle as a durable workflow and return its handle.

    Imported lazily and constructed on demand: the engine registers globally
    and needs a live database, neither of which the offline test suite or the
    offline experiments should require in order to import this module.
    """
    from dbos import DBOS, DBOSConfig  # noqa: PLC0415 -- needs a database; import on use

    dbos_config: DBOSConfig = config.as_dbos_config()
    DBOS(config=dbos_config)

    @DBOS.step()
    def durable_submit(client, submission: ImsSubmission) -> SubmitResult:
        return submit_step(client, submission)

    @DBOS.workflow()
    def cycle(dataset, client, router, settings, *, as_of: date, recorded_at: datetime):
        from gst_recon.workflow.pipeline import run_cycle  # noqa: PLC0415 -- avoids a cycle

        return run_cycle(dataset, client, router, settings, as_of=as_of, recorded_at=recorded_at)

    DBOS.launch()
    return cycle, durable_submit


def recover_incomplete(limit: int = 100) -> list[str]:
    """Resume every workflow left unfinished by a crash.

    Filters on both recoverable statuses. Filtering on PENDING alone returns an
    empty list after a hard kill and reads as "nothing to recover", which is
    the most misleading possible answer for a system whose failure mode is
    silent acceptance.
    """
    from dbos import DBOS  # noqa: PLC0415 -- needs a database; import on use

    pending = DBOS.list_workflows(status=list(RECOVERABLE_STATUSES), limit=limit)
    resumed = []
    for status in pending:
        DBOS.resume_workflow(status.workflow_id)
        resumed.append(status.workflow_id)
    return resumed
