"""Turning a register file into purchase lines, and saying what it dropped.

Two failure levels, deliberately kept apart.

**File level is fatal.** If the header cannot be mapped, or the date column's
convention cannot be resolved, nothing is loaded. These are errors about how to
read *every* row, so producing a partial result would mean producing rows read
under an assumption nobody checked.

**Row level is survivable and reported.** One row with a mangled amount should
not cost a client their whole register. Bad rows are collected with their
spreadsheet row number and the reason, and the good ones proceed -- but the
count is returned rather than logged, because a caller that never learns it
dropped forty rows will reconcile a register that is quietly incomplete, and an
inward document missing at the cut-off is treated as accepted.

A rejected row is rejected whole. Filling a failed field with a default and
keeping the rest would put a document into the reconciliation carrying a number
nobody supplied.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from gst_recon.domain.money import TaxAmounts
from gst_recon.domain.records import DocumentKind, PurchaseLine
from gst_recon.ingest import coerce, schema
from gst_recon.ingest.xlsx import XlsxError, read_sheet

# Header row plus zero-based body index -> the row number a person sees in
# Excel. Off-by-one here would send someone to the wrong line of a 4,000-row
# register, which is worse than giving them no number at all.
HEADER_ROWS = 1

_KIND_WORDS: dict[str, DocumentKind] = {
    "invoice": DocumentKind.INVOICE,
    "inv": DocumentKind.INVOICE,
    "b2b": DocumentKind.INVOICE,
    "creditnote": DocumentKind.CREDIT_NOTE,
    "credit": DocumentKind.CREDIT_NOTE,
    "cn": DocumentKind.CREDIT_NOTE,
    "cdnr": DocumentKind.CREDIT_NOTE,
    "debitnote": DocumentKind.DEBIT_NOTE,
    "debit": DocumentKind.DEBIT_NOTE,
    "dn": DocumentKind.DEBIT_NOTE,
    "amendment": DocumentKind.AMENDMENT,
    "amended": DocumentKind.AMENDMENT,
}


class IngestRefusedError(ValueError):
    """The file cannot be read at all, and why."""


@dataclass(frozen=True, slots=True)
class RowRejection:
    """One row that could not be turned into a document."""

    row: int
    reason: str
    values: tuple[str, ...]


@dataclass(slots=True)
class IngestReport:
    """What was loaded, what was refused, and how the file was interpreted."""

    lines: list[PurchaseLine] = field(default_factory=list)
    rejected: list[RowRejection] = field(default_factory=list)
    mapping: schema.ColumnMap = field(default_factory=schema.ColumnMap)
    date_order: coerce.DateColumn = field(
        default_factory=lambda: coerce.DateColumn(coerce.DateOrder.UNRESOLVED, "")
    )
    source: str = ""

    @property
    def rows_seen(self) -> int:
        return len(self.lines) + len(self.rejected)

    @property
    def rejection_rate(self) -> float:
        return len(self.rejected) / self.rows_seen if self.rows_seen else 0.0

    def summary(self) -> str:
        parts = [f"{len(self.lines)} of {self.rows_seen} rows loaded from {self.source or 'input'}"]
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected")
        if self.mapping.unrecognised:
            unknown = ", ".join(sorted(self.mapping.unrecognised.values()))
            parts.append(f"columns ignored as unrecognised: {unknown}")
        if self.date_order.evidence:
            reading = self.date_order.order.value.lower()
            parts.append(f"dates read {reading} ({self.date_order.evidence})")
        return "; ".join(parts)


def read_table(path: Path | str, sheet_name: str | None = None) -> list[list[str]]:
    """Read a register as rows of text, whatever container it arrived in."""
    location = Path(path)
    if location.suffix.lower() in {".csv", ".txt"}:
        with location.open(newline="", encoding="utf-8-sig") as handle:
            # utf-8-sig: a CSV exported from Excel leads with a byte-order mark,
            # which would otherwise become part of the first column's heading
            # and make it unrecognisable.
            return [list(row) for row in csv.reader(handle)]
    if location.suffix.lower() == ".xlsx":
        return read_sheet(location, sheet_name).rows
    raise IngestRefusedError(f"{location.name}: expected .xlsx or .csv")


def _is_numeric_date(text: str) -> bool:
    """A slash- or dash-separated date, whose reading depends on a convention.

    An ISO value is unambiguous by construction, so a column made entirely of
    them needs no convention and must not be refused for lacking one.
    """
    try:
        coerce.parse_date(text, coerce.DateOrder.DAY_FIRST)
    except coerce.CoercionError:
        return False
    return not text.strip()[:4].isdigit()


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def _kind(text: str) -> DocumentKind:
    key = schema.normalise_heading(text)
    if not key:
        return DocumentKind.INVOICE
    kind = _KIND_WORDS.get(key)
    if kind is None:
        raise coerce.CoercionError(f"{text!r} is not a document type this reader knows")
    return kind


def _tax_amounts(row: list[str], mapping: schema.ColumnMap) -> TaxAmounts:
    return TaxAmounts(
        taxable=coerce.parse_amount(_cell(row, mapping.index("taxable"))),
        cgst=coerce.parse_decimal_or_zero(_cell(row, mapping.index("cgst"))),
        sgst=coerce.parse_decimal_or_zero(_cell(row, mapping.index("sgst"))),
        igst=coerce.parse_decimal_or_zero(_cell(row, mapping.index("igst"))),
        cess=coerce.parse_decimal_or_zero(_cell(row, mapping.index("cess"))),
    )


def _line(
    row: list[str],
    row_number: int,
    mapping: schema.ColumnMap,
    order: coerce.DateOrder,
) -> PurchaseLine:
    gstin = coerce.normalise_identifier(_cell(row, mapping.index("supplier_gstin")))
    number = coerce.normalise_identifier(_cell(row, mapping.index("invoice_number")))
    if not gstin:
        raise coerce.CoercionError("supplier GSTIN is blank")
    if not number:
        raise coerce.CoercionError("invoice number is blank")

    tax = _tax_amounts(row, mapping)
    if not tax.well_formed_split:
        raise coerce.CoercionError(
            "both intra-state and inter-state tax are populated, which no single "
            "document carries -- the row is describing two supplies or the wrong columns"
        )

    original = coerce.normalise_identifier(_cell(row, mapping.index("original_invoice_number")))
    irn = coerce.normalise_identifier(_cell(row, mapping.index("irn")))
    return PurchaseLine(
        line_id=f"R{row_number:05d}",
        supplier_gstin=gstin,
        invoice_number=number,
        invoice_date=coerce.parse_date(_cell(row, mapping.index("invoice_date")), order),
        tax=tax,
        kind=_kind(_cell(row, mapping.index("kind"))),
        reverse_charge=coerce.parse_flag(_cell(row, mapping.index("reverse_charge"))),
        irn=irn or None,
        original_invoice_number=original or None,
        source_row=row_number,
    )


def load_purchase_register(
    path: Path | str,
    sheet_name: str | None = None,
    date_order: coerce.DateOrder | None = None,
) -> IngestReport:
    """Read a purchase register, or refuse the file and say what is wrong.

    ``date_order`` is the escape hatch for a file whose own dates cannot settle
    the question -- every day in it falls on or before the 12th. Supplying it is
    an assertion by whoever knows the file's provenance, which is a different
    thing from this module assuming a locale.
    """
    location = Path(path)
    try:
        rows = read_table(location, sheet_name)
    except XlsxError as exc:
        raise IngestRefusedError(str(exc)) from exc

    if len(rows) < HEADER_ROWS + 1:
        raise IngestRefusedError(f"{location.name} has a header and no rows")

    mapping = schema.map_headers(rows[0])
    if not mapping.usable:
        raise IngestRefusedError(
            f"{location.name} cannot be mapped:\n  - " + "\n  - ".join(mapping.problems)
        )

    body = rows[HEADER_ROWS:]
    date_index = mapping.index("invoice_date")
    resolved = (
        coerce.DateColumn(date_order, "supplied by the caller")
        if date_order is not None
        else coerce.infer_date_order([_cell(row, date_index) for row in body])
    )

    if not resolved.resolved:
        ambiguous = [
            text
            for row in body
            if (text := _cell(row, date_index).strip()) and _is_numeric_date(text)
        ]
        if ambiguous:
            raise IngestRefusedError(
                f"{location.name}: the invoice date column cannot be read. Every value in it "
                f"is ambiguous -- {ambiguous[0]!r} is a valid date read either way -- "
                + (f"and {resolved.evidence}. " if resolved.evidence else "")
                + "Supply the date order explicitly rather than have one guessed, because "
                "a date read the wrong way round changes which documents are time-barred."
            )

    report = IngestReport(mapping=mapping, date_order=resolved, source=location.name)
    for offset, row in enumerate(body):
        row_number = offset + HEADER_ROWS + 1
        if not any(cell.strip() for cell in row):
            continue
        try:
            report.lines.append(_line(row, row_number, mapping, resolved.order))
        except (coerce.CoercionError, ValueError) as exc:
            report.rejected.append(RowRejection(row_number, str(exc), tuple(row)))

    if report.lines or not report.rejected:
        return report

    # Every row failing is not forty separate row problems. It is one file
    # problem wearing forty hats, and reporting it row by row would bury that.
    raise IngestRefusedError(
        f"{location.name}: every row was rejected, so the file is not what its header claims. "
        f"First reason: {report.rejected[0].reason}"
    )
