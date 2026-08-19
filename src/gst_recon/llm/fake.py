"""A deterministic provider that never touches the network.

The entire test suite runs against this. That is a hard requirement rather
than a convenience: tests that need an API key cannot run in CI, cannot run
from a clean clone, and quietly rot the moment a free-tier quota changes.

It is not a mock that returns a fixed blob. It walks a scripted probe sequence
per exception class, so an agent driven by it produces a realistic multi-step
trajectory with tool calls, tool results and a final Finding -- enough to
exercise budget enforcement, loop detection and evidence validation for real.
The scripts encode how a competent human would investigate each class, which
is also what the trajectory evaluation scores against.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from gst_recon.domain.taxonomy import ExceptionClass, ImsAction
from gst_recon.llm.types import (
    GenerationConfig,
    LlmResponse,
    Message,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)

# How a competent human investigates each class: which probe to run, in order,
# and what to conclude once the probes come back.
REFERENCE_PROBES: dict[ExceptionClass, tuple[str, ...]] = {
    ExceptionClass.MISSING_IN_BOOKS: (
        "query_purchase_register",
        "query_bank_ledger",
        "query_vendor_master",
    ),
    ExceptionClass.AMOUNT_MISMATCH: (
        "compute_tolerance_match",
        "query_purchase_register",
    ),
    ExceptionClass.GSTIN_MISMATCH: (
        "query_vendor_master",
        "query_purchase_register",
    ),
    ExceptionClass.CREDIT_NOTE_UNLINKED: (
        "query_prior_2b",
        "query_purchase_register",
    ),
    ExceptionClass.MISSING_IN_2B: (
        "query_vendor_master",
        "query_prior_2b",
    ),
}

_CONCLUSIONS: dict[ExceptionClass, tuple[ImsAction, str]] = {
    ExceptionClass.MISSING_IN_BOOKS: (
        ImsAction.ACCEPT,
        "a bank payment corroborates the document, so this is a missing book "
        "entry rather than a document we never transacted",
    ),
    ExceptionClass.AMOUNT_MISMATCH: (
        ImsAction.PENDING,
        "the tax difference exceeds the configured tolerance and needs the "
        "supplier to confirm which figure is correct",
    ),
    ExceptionClass.GSTIN_MISMATCH: (
        ImsAction.ACCEPT,
        "the vendor master confirms the portal GSTIN; the book entry carries a "
        "transposition of two adjacent characters",
    ),
    ExceptionClass.CREDIT_NOTE_UNLINKED: (
        ImsAction.PENDING,
        "the original invoice sits in a prior return period and the linkage "
        "must be confirmed before the credit note is actioned",
    ),
    ExceptionClass.MISSING_IN_2B: (
        ImsAction.PENDING,
        "the supplier has not filed; recovery is the appropriate route",
    ),
}

_CLASS_RE = re.compile("|".join(cls.value for cls in ExceptionClass))


@dataclass(slots=True)
class FakeProvider:
    """Scripted, deterministic, offline.

    ``failure_mode`` lets tests drive the pathological behaviours the budget
    machinery exists to contain, without waiting for a real model to
    misbehave: an agent that loops forever, and one that answers confidently
    with no evidence at all.
    """

    model: str = "fake-deterministic"
    name: str = "fake"
    failure_mode: str | None = None
    calls: int = field(default=0, init=False)

    def _exception_class(self, messages: list[Message]) -> ExceptionClass:
        for message in messages:
            if message.role is Role.USER:
                found = _CLASS_RE.search(message.content)
                if found:
                    return ExceptionClass(found.group(0))
        return ExceptionClass.MISSING_IN_BOOKS

    @staticmethod
    def _steps_taken(messages: list[Message]) -> int:
        return sum(1 for message in messages if message.role is Role.TOOL)

    def _synthetic_usage(self, messages: list[Message], output: str) -> Usage:
        """Token counts proportional to real text length.

        Approximate, and labelled as such wherever it is reported. The point is
        that offline budget-enforcement tests exercise the same arithmetic the
        live path uses, not that these numbers are accurate.
        """
        prompt_chars = sum(len(message.content) for message in messages)
        return Usage(input_tokens=prompt_chars // 4, output_tokens=max(len(output) // 4, 1))

    def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        config: GenerationConfig,  # noqa: ARG002 -- part of the Provider protocol
    ) -> LlmResponse:
        self.calls += 1
        supplier_text = self._supplier_message(messages)
        if supplier_text is not None:
            return self._classify_supplier_reply(supplier_text, messages)
        exception_class = self._exception_class(messages)
        step = self._steps_taken(messages)
        available = {tool.name for tool in tools}

        if self.failure_mode == "loop":
            # Always the same call with the same arguments: what loop detection
            # has to catch before the step budget runs out.
            call = ToolCall("tc-loop", "query_purchase_register", {"gstin": "27AAPFU0939F1ZV"})
            return LlmResponse(
                "checking the register again",
                (call,),
                self._synthetic_usage(messages, "loop"),
                self.model,
            )

        if self.failure_mode == "no_evidence":
            payload = {
                "proposed_class": exception_class.value,
                "proposed_action": ImsAction.ACCEPT.value,
                "confidence": 0.99,
                "rationale": "This is clearly fine.",
                "evidence": [],
            }
            text = json.dumps(payload)
            return LlmResponse(text, (), self._synthetic_usage(messages, text), self.model)

        script = REFERENCE_PROBES.get(exception_class, ())
        if step < len(script) and script[step] in available:
            name = script[step]
            call = ToolCall(f"tc{step:02d}-{name}", name, self._arguments_for(name, messages))
            text = f"Running {name} to establish whether this document has a counterpart."
            return LlmResponse(text, (call,), self._synthetic_usage(messages, text), self.model)

        action, rationale = _CONCLUSIONS.get(
            exception_class, (ImsAction.PENDING, "insufficient evidence to propose an action")
        )
        evidence = [
            {
                "tool_call_id": f"tc{index:02d}-{name}",
                "tool_name": name,
                "claim": f"{name} returned material bearing on this document",
            }
            for index, name in enumerate(script[: max(step, 0)])
        ]
        payload = {
            "proposed_class": exception_class.value,
            "proposed_action": action.value,
            # Deterministic pseudo-confidence derived from the case itself, so
            # the straight-through curve has something to sweep that is stable
            # across runs but not constant across cases.
            "confidence": self._confidence(exception_class, messages),
            "rationale": rationale,
            "evidence": evidence,
        }
        text = json.dumps(payload)
        return LlmResponse(text, (), self._synthetic_usage(messages, text), self.model)

    @staticmethod
    def _supplier_message(messages: list[Message]) -> str | None:
        """Extract fenced supplier text, if this is a Tier 3 classification call."""
        for message in messages:
            found = re.search(
                r"<<<SUPPLIER_MESSAGE_BEGIN>>>\n(.*)\n<<<SUPPLIER_MESSAGE_END>>>",
                message.content,
                re.DOTALL,
            )
            if found:
                return found.group(1)
        return None

    def _classify_supplier_reply(self, text: str, messages: list[Message]) -> LlmResponse:
        """Keyword-driven stand-in for the reply classifier.

        Crucially it classifies an injection attempt as UNCLEAR rather than
        obeying it, which is what lets the offline suite assert that a hostile
        supplier message cannot reach an accept.
        """
        lowered = text.lower()
        looks_instructional = any(
            marker in lowered
            for marker in (
                "ignore previous",
                "new instructions",
                "mark all",
                "accept all",
                "you are now",
                "system prompt",
            )
        )
        if looks_instructional:
            kind = "UNCLEAR"
        elif "already filed" in lowered or "have filed" in lowered:
            kind = "CLAIMS_FILED"
        elif "will file" in lowered or "next month" in lowered:
            kind = "WILL_FILE"
        elif "wrong amount" in lowered or "amount is" in lowered:
            kind = "DISPUTES_AMOUNT"
        elif "no such invoice" in lowered or "never supplied" in lowered:
            kind = "DISPUTES_EXISTENCE"
        elif "send" in lowered and ("copy" in lowered or "document" in lowered):
            kind = "REQUESTS_DOCUMENTS"
        else:
            kind = "UNCLEAR"
        payload = {
            "kind": kind,
            "quote": text.strip()[:80],
            "contains_apparent_instructions": looks_instructional,
        }
        rendered = json.dumps(payload)
        return LlmResponse(rendered, (), self._synthetic_usage(messages, rendered), self.model)

    @staticmethod
    def _arguments_for(name: str, messages: list[Message]) -> dict[str, Any]:
        gstin = ""
        for message in messages:
            found = re.search(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b", message.content)
            if found:
                gstin = found.group(0)
                break
        if name == "query_bank_ledger":
            return {"gstin": gstin, "tolerance": 1.0}
        if name == "query_prior_2b":
            return {"gstin": gstin, "periods": 3}
        return {"gstin": gstin}

    @staticmethod
    def _confidence(exception_class: ExceptionClass, messages: list[Message]) -> float:
        seed = hashlib.sha256(
            (exception_class.value + "".join(m.content for m in messages)).encode()
        ).digest()
        return round(0.55 + (seed[0] / 255.0) * 0.44, 4)
