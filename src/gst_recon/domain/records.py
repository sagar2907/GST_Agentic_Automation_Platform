"""Document records for both sides of the reconciliation.

Two sides, deliberately kept as separate types rather than one union: a book
line is something the taxpayer asserts, a 2B line is something the government
asserts on the strength of a supplier's filing. Conflating them loses the
provenance that every downstream decision depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from gst_recon.domain.money import TaxAmounts
from gst_recon.domain.taxonomy import ExceptionClass, ImsAction


class DocumentKind(StrEnum):
    INVOICE = "INVOICE"
    CREDIT_NOTE = "CREDIT_NOTE"
    DEBIT_NOTE = "DEBIT_NOTE"
    AMENDMENT = "AMENDMENT"


class IrnStatus(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class PurchaseLine:
    """A line the taxpayer recorded in their own purchase register."""

    line_id: str
    supplier_gstin: str
    invoice_number: str
    invoice_date: date
    tax: TaxAmounts
    kind: DocumentKind = DocumentKind.INVOICE
    reverse_charge: bool = False
    irn: str | None = None
    original_invoice_number: str | None = None
    source_row: int | None = None


@dataclass(frozen=True, slots=True)
class Gstr2bLine:
    """A line the government generated from the supplier's GSTR-1 filing."""

    line_id: str
    supplier_gstin: str
    invoice_number: str
    invoice_date: date
    tax: TaxAmounts
    kind: DocumentKind = DocumentKind.INVOICE
    reverse_charge: bool = False
    irn: str | None = None
    irn_status: IrnStatus = IrnStatus.NOT_APPLICABLE
    original_invoice_number: str | None = None
    return_period: str = ""
    filed_late: bool = False


@dataclass(frozen=True, slots=True)
class MatchReason:
    """Why two lines were considered the same document.

    An unauditable match is worthless in a tax context, so every non-exact
    match records the rule that produced it and the residual difference that
    rule tolerated. This is what a reviewer reads when they disagree.
    """

    rule: str
    detail: str
    tax_delta: Decimal
    days_apart: int = 0
    number_similarity: float = 1.0


@dataclass(frozen=True, slots=True)
class MatchedPair:
    book: PurchaseLine
    portal: Gstr2bLine
    reason: MatchReason
    exact: bool


@dataclass(frozen=True, slots=True)
class ExceptionRecord:
    """One unresolved document, carrying everything a decision needs.

    Exactly one of book/portal is populated for the two "missing" classes; both
    are populated when the two sides disagree about a document they both hold.
    """

    exception_id: str
    exception_class: ExceptionClass
    book: PurchaseLine | None = None
    portal: Gstr2bLine | None = None
    detail: str = ""
    candidates: tuple[str, ...] = ()

    @property
    def supplier_gstin(self) -> str:
        for side in (self.portal, self.book):
            if side is not None:
                return side.supplier_gstin
        raise ValueError(f"exception {self.exception_id} has neither side populated")

    @property
    def amount_at_risk(self) -> Decimal:
        for side in (self.portal, self.book):
            if side is not None:
                return side.tax.total_tax
        raise ValueError(f"exception {self.exception_id} has neither side populated")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """A single cited fact, bound to the tool call that produced it.

    The tool_call_id is not decoration. A Finding whose evidence cannot be
    traced back to a recorded tool result is rejected by the validator, which
    is the structural answer to a model that produces a fluent but invented
    justification.
    """

    tool_call_id: str
    tool_name: str
    claim: str


@dataclass(frozen=True, slots=True)
class Finding:
    """What the investigation agent produces. Never an action -- a proposal."""

    exception_id: str
    proposed_class: ExceptionClass
    proposed_action: ImsAction
    confidence: float
    rationale: str
    evidence: tuple[EvidenceItem, ...] = ()
    steps_used: int = 0
    terminated_because: str = "completed"
    precedent_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_articulable_reason(self) -> bool:
        """A rejection we cannot explain is not a rejection we may make."""
        return bool(self.rationale.strip()) and bool(self.evidence)
