"""GSTN access through a GSP, and the idempotent submit that guards it.

Nothing in this repository has ever talked to GSTN. Sandbox access runs through
a licensed GST Suvidha Provider and needs a developer registration this build
never made, so what exists here is the interface a real client would implement
and a fake that conforms to the published request and response shapes.

That is worth building even without credentials, because the interesting
engineering is not the HTTP. It is the submit path.

**Why idempotency is not optional here.** Measured directly against the durable
workflow engine: after a process is killed mid-workflow, completed steps are
memoised and never re-run, but *the step that was interrupted runs again* on
resume. So a crash between "the portal accepted this action" and "we recorded
that it did" leaves a step that will be retried against a portal which has
already applied it.

For an IMS action that matters more than usual. A reject purges the invoice
value from GSTR-2B for the period and cannot be undone within the cycle, so a
duplicate submit is not a harmless repeat.

**Why the key is derived, not generated.** The idempotency key is a hash of the
decision content -- taxpayer, period, document, action, ruleset version. A
random key would be a different key on the retry, which is precisely when it
needs to be the same one. Deriving it means a resumed step computes the
identical key, the ledger recognises the work as already done, and the original
outcome is replayed instead of a second action being taken.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Protocol

from gst_recon.domain.taxonomy import ImsAction


class SubmitOutcome(StrEnum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    REJECTED_BY_PORTAL = "REJECTED_BY_PORTAL"


@dataclass(frozen=True, slots=True)
class ImsSubmission:
    """One action to take against one inward document."""

    taxpayer_gstin: str
    return_period: str
    document_id: str
    action: ImsAction
    ruleset_version: str

    def idempotency_key(self) -> str:
        """A key the same decision always produces, on every attempt.

        Deliberately excludes anything that varies between attempts -- no
        timestamp, no attempt counter, no random component. Two invocations of
        the same decision must collide, because that collision is the whole
        mechanism.
        """
        material = "|".join(
            (
                self.taxpayer_gstin,
                self.return_period,
                self.document_id,
                self.action.value,
                self.ruleset_version,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class SubmitResult:
    outcome: SubmitOutcome
    idempotency_key: str
    portal_reference: str
    detail: str = ""

    @property
    def took_effect(self) -> bool:
        """Did *this* call change anything at the portal?

        False for a replay. Callers that count actions taken must use this
        rather than treating every non-error response as a fresh action.
        """
        return self.outcome is SubmitOutcome.APPLIED


class GstnClient(Protocol):
    """What the workflow needs from GSTN. Deliberately small."""

    def fetch_gstr2b(self, taxpayer_gstin: str, return_period: str) -> list[dict]: ...

    def fetch_ims_inbox(self, taxpayer_gstin: str, return_period: str) -> list[dict]: ...

    def submit_ims_action(self, submission: ImsSubmission) -> SubmitResult: ...


@dataclass(slots=True)
class SubmissionLedger:
    """Remembers which keys have already been applied.

    In a deployment this is a table with a unique constraint on the key, so two
    concurrent workers racing the same submission lose one insert rather than
    sending two actions. Held in memory here because the fake portal is
    in-process, and the semantics being demonstrated are the same.
    """

    applied: dict[str, SubmitResult] = field(default_factory=dict)

    def seen(self, key: str) -> SubmitResult | None:
        return self.applied.get(key)

    def record(self, key: str, result: SubmitResult) -> None:
        self.applied.setdefault(key, result)


@dataclass(slots=True)
class FakeGspClient:
    """An in-process stand-in shaped like a GSP's IMS API.

    ``fail_after`` makes the portal drop the connection after a set number of
    successful submissions -- the failure that motivates all of this, where the
    action is applied and the caller never learns it.
    """

    ledger: SubmissionLedger = field(default_factory=SubmissionLedger)
    portal_state: dict[str, str] = field(default_factory=dict)
    fail_after: int | None = None
    calls: int = 0

    def fetch_gstr2b(self, taxpayer_gstin: str, return_period: str) -> list[dict]:
        return [{"taxpayer_gstin": taxpayer_gstin, "return_period": return_period, "docs": []}]

    def fetch_ims_inbox(self, taxpayer_gstin: str, return_period: str) -> list[dict]:
        # Scoped by taxpayer and period like the real inbox, so a caller that
        # forgets to pass them gets an empty list here rather than silently
        # working against a fake that ignores scoping and a portal that does not.
        return [
            {
                "taxpayer_gstin": taxpayer_gstin,
                "return_period": return_period,
                "document_id": key,
                "current_action": value,
            }
            for key, value in sorted(self.portal_state.items())
        ]

    def submit_ims_action(self, submission: ImsSubmission) -> SubmitResult:
        key = submission.idempotency_key()

        # The duplicate check happens before the side effect, and before the
        # injected failure, because that is the order a real portal keying on
        # the header would use.
        previous = self.ledger.seen(key)
        if previous is not None:
            return SubmitResult(
                SubmitOutcome.ALREADY_APPLIED,
                key,
                previous.portal_reference,
                "replayed; the portal had already applied this action",
            )

        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError("portal closed the connection")

        self.portal_state[submission.document_id] = submission.action.value
        result = SubmitResult(SubmitOutcome.APPLIED, key, f"REF-{key[:12]}", "action applied")
        self.ledger.record(key, result)
        return result


def submit_once(client: GstnClient, submission: ImsSubmission) -> SubmitResult:
    """Submit an action, tolerating a retry of work already done."""
    return client.submit_ims_action(submission)


def cutoff_for(period: str, cutoff_day: int) -> date:
    """The 2B generation date for an MM-YYYY period."""
    month, year = period.split("-")
    return date(int(year), int(month), cutoff_day)
