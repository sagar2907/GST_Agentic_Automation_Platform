"""The immutable audit record.

Every decision this system takes is written here before it is acted on, and
nothing here is ever updated in place. That is not bookkeeping fussiness: a
wrong action creates tax exposure for a live business, and the question a
reviewer asks six months later is never "what does the system think now" but
"what did it know when it decided, and who agreed".

Three properties the rest of the system depends on.

**Append-only.** There is no update and no delete. A correction is a new entry
that supersedes an earlier one, so the sequence of what was believed over time
survives. An audit log you can edit is a log that proves nothing.

**Self-describing.** Each entry carries the ruleset version, the model that
produced the finding, and a hash of the prompt that produced it. Six months on,
"the agent said accept" is useless; "ruleset 2026.07, gemini-3.5-flash-lite,
prompt hash 3f9a..." can be reproduced.

**Hash-chained.** Each entry embeds the digest of the one before it, so a
deleted or altered row breaks the chain and ``verify_chain`` says where. This
does not stop someone with database access from rewriting history, and it is
not meant to -- it makes silent partial tampering detectable, which is the
realistic threat for an audit trail nobody reads until it matters.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def prompt_hash(text: str) -> str:
    """Stable digest of a prompt, so a decision can be traced to its input."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One decision, frozen at the moment it was taken.

    ``recorded_at`` should be timezone-aware. A naive timestamp cannot be
    ordered against one recorded on another machine or in another season, and
    ordering is most of what an audit trail is for -- a filing cut-off argument
    turns on which of two things happened first.

    It is passed in rather than read from the clock inside this
    module. Every other component here takes its time as a parameter so it can
    be tested against a frozen clock, and the audit log is the last place that
    should make an exception -- a log whose timestamps cannot be reproduced in
    a test is a log whose ordering cannot be tested either.
    """

    sequence: int
    exception_id: str
    action: str
    requires_human: bool
    reasons: tuple[str, ...]
    recorded_at: datetime
    ruleset_version: str
    model: str = ""
    prompt_digest: str = ""
    confidence: float | None = None
    evidence_call_ids: tuple[str, ...] = ()
    approver: str | None = None
    amount_at_risk: Decimal | None = None
    previous_digest: str = GENESIS
    superseded_by: int | None = None

    def body(self) -> dict[str, Any]:
        payload = asdict(self)
        # The digest covers everything except the digest field itself and the
        # supersession pointer, which is written later by a subsequent entry.
        payload.pop("superseded_by", None)
        return payload

    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.body()).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class AuditLog:
    """An append-only, hash-chained sequence of decisions."""

    entries: list[AuditEntry] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def head_digest(self) -> str:
        return self.entries[-1].digest() if self.entries else GENESIS

    def append(
        self,
        *,
        exception_id: str,
        action: str,
        requires_human: bool,
        reasons: tuple[str, ...],
        recorded_at: datetime,
        ruleset_version: str,
        model: str = "",
        prompt_digest: str = "",
        confidence: float | None = None,
        evidence_call_ids: tuple[str, ...] = (),
        approver: str | None = None,
        amount_at_risk: Decimal | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            sequence=len(self.entries),
            exception_id=exception_id,
            action=action,
            requires_human=requires_human,
            reasons=reasons,
            recorded_at=recorded_at,
            ruleset_version=ruleset_version,
            model=model,
            prompt_digest=prompt_digest,
            confidence=confidence,
            evidence_call_ids=evidence_call_ids,
            approver=approver,
            amount_at_risk=amount_at_risk,
            previous_digest=self.head_digest,
        )
        self.entries.append(entry)
        return entry

    def supersede(self, sequence: int, replacement: AuditEntry) -> None:
        """Point an earlier entry at the one that replaced it.

        The original is not rewritten -- its content and digest stand. Only the
        forward pointer is set, so the chain still verifies and a reader can
        see both what was decided and that it was later revised.
        """
        original = self.entries[sequence]
        self.entries[sequence] = AuditEntry(
            **{**original.body(), "superseded_by": replacement.sequence}
        )

    def for_exception(self, exception_id: str) -> list[AuditEntry]:
        return [entry for entry in self.entries if entry.exception_id == exception_id]

    def verify_chain(self) -> tuple[bool, str]:
        """Walk the chain and report the first break, if any."""
        expected = GENESIS
        for index, entry in enumerate(self.entries):
            if entry.sequence != index:
                return False, f"entry at position {index} claims sequence {entry.sequence}"
            if entry.previous_digest != expected:
                return False, f"entry {index} does not follow entry {index - 1}"
            expected = entry.digest()
        return True, "chain intact"

    def write_jsonl(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for entry in self.entries:
                payload = entry.body()
                payload["digest"] = entry.digest()
                payload["superseded_by"] = entry.superseded_by
                handle.write(_canonical(payload) + "\n")
        return path
