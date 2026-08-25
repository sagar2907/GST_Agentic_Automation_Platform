"""Mapping a real register's column headings onto the fields this system needs.

Nobody agrees on what to call these columns. The same field arrives as
``GSTIN``, ``Supplier GSTIN``, ``GSTIN of Supplier``, ``Party GSTIN`` or
``gstin_no`` depending on which accounting package wrote the file, so a fixed
column order is not an option and neither is asking every client to reformat.

Three decisions shape what follows.

**The mapping is deterministic, not inferred by a model.** A model asked to
match headings would be right almost always, and the failure it produces when
it is wrong -- a confident mapping of the wrong column -- is invisible. This is
a lookup table, so when it fails it fails by finding nothing, which is loud.

**Columns that must not be used are recognised too.** A register commonly
carries the taxpayer's own GSTIN alongside the supplier's. A table that only
knew supplier aliases would see ``GSTIN`` in the heading ``Recipient GSTIN``
and map the taxpayer's own registration as the supplier for every row. Naming
the columns to reject is what stops a loose alias from reaching them.

**Two candidates for one field is a refusal, not a tie-break.** If both
``Supplier GSTIN`` and ``Party GSTIN`` are present, the file means something
by the distinction and this module does not know what. Picking the leftmost
would work on most files and be wrong on the rest, without saying which.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NOISE = re.compile(r"[^a-z0-9]+")

# Field name -> the headings that mean it. Written most specific first for
# readability only; matching is exact against the normalised heading, so order
# carries no meaning and adding an alias cannot shadow an existing one.
ALIASES: dict[str, tuple[str, ...]] = {
    "supplier_gstin": (
        "suppliergstin",
        "gstinofsupplier",
        "vendorgstin",
        "partygstin",
        "sellergstin",
        "gstinno",
        "gstinnumber",
        "gstin",
    ),
    "invoice_number": (
        "invoiceno",
        "invoicenumber",
        "invno",
        "billno",
        "billnumber",
        "documentno",
        "documentnumber",
        "docno",
        "referenceno",
        "voucherno",
    ),
    "invoice_date": (
        "invoicedate",
        "invdate",
        "billdate",
        "documentdate",
        "docdate",
        "dateofinvoice",
        "voucherdate",
        "date",
    ),
    "taxable": (
        "taxablevalue",
        "taxableamount",
        "assessablevalue",
        "basicamount",
        "netamount",
        "taxable",
    ),
    "cgst": ("cgst", "cgstamount", "cgstamt", "centraltax", "centralgst"),
    "sgst": ("sgst", "sgstamount", "sgstamt", "statetax", "stategst", "utgst", "utgstamount"),
    "igst": ("igst", "igstamount", "igstamt", "integratedtax", "integratedgst"),
    "cess": ("cess", "cessamount", "cessamt"),
    "kind": ("documenttype", "doctype", "invoicetype", "type", "notetype"),
    "reverse_charge": (
        "reversecharge",
        "rcm",
        "reversechargeapplicable",
        "issupplyattractingreversecharge",
        "revchrg",
    ),
    "irn": ("irn", "irnno", "irnnumber", "einvoiceirn"),
    "original_invoice_number": (
        "originalinvoiceno",
        "originalinvoicenumber",
        "againstinvoiceno",
        "originaldocumentno",
        "originalinvno",
    ),
    "place_of_supply": ("placeofsupply", "pos", "posstate", "supplystate"),
}

# Headings that look like fields above but are somebody else's identity or a
# derived total. Claimed here so a loose alias can never reach them.
IGNORED: dict[str, tuple[str, ...]] = {
    "recipient identity": (
        "recipientgstin",
        "buyergstin",
        "ourgstin",
        "customergstin",
        "gstinofrecipient",
        "companygstin",
        "purchasergstin",
    ),
    "derived total": ("totaltax", "invoicevalue", "totalamount", "grandtotal", "totalinvoicevalue"),
}

REQUIRED = ("supplier_gstin", "invoice_number", "invoice_date", "taxable")
TAX_HEADS = ("cgst", "sgst", "igst", "cess")


def normalise_heading(heading: str) -> str:
    """``"GSTIN of Supplier "`` -> ``"gstinofsupplier"``."""
    return _NOISE.sub("", heading.strip().lower())


_BY_HEADING: dict[str, str] = {
    alias: name for name, aliases in ALIASES.items() for alias in aliases
}
_IGNORED_HEADINGS: dict[str, str] = {
    alias: reason for reason, aliases in IGNORED.items() for alias in aliases
}


@dataclass(frozen=True, slots=True)
class ColumnMap:
    """The outcome of reading a header row.

    ``problems`` is non-empty exactly when the file must not be ingested. It
    holds sentences rather than codes because its only consumer is a person
    deciding what to fix in a spreadsheet.
    """

    columns: dict[str, int] = field(default_factory=dict)
    ignored: dict[int, str] = field(default_factory=dict)
    unrecognised: dict[int, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.problems

    def index(self, name: str) -> int | None:
        return self.columns.get(name)


def map_headers(headings: list[str]) -> ColumnMap:
    """Map a header row, or explain precisely why it cannot be mapped."""
    candidates: dict[str, list[tuple[int, str]]] = {}
    ignored: dict[int, str] = {}
    unrecognised: dict[int, str] = {}

    for position, heading in enumerate(headings):
        key = normalise_heading(heading)
        if not key:
            continue
        if key in _IGNORED_HEADINGS:
            ignored[position] = _IGNORED_HEADINGS[key]
            continue
        name = _BY_HEADING.get(key)
        if name is None:
            unrecognised[position] = heading
            continue
        candidates.setdefault(name, []).append((position, heading))

    problems: list[str] = []
    columns: dict[str, int] = {}
    ambiguous: set[str] = set()
    for name, found in sorted(candidates.items()):
        if len(found) > 1:
            ambiguous.add(name)
            shown = " and ".join(repr(heading) for _, heading in found)
            problems.append(
                f"{shown} both mean {name}; the file distinguishes them and this does not, "
                "so rename or remove one rather than have one chosen arbitrarily"
            )
            continue
        columns[name] = found[0][0]

    seen = ", ".join(repr(heading) for heading in headings if heading.strip())
    for name in REQUIRED:
        if name not in columns and name not in ambiguous:
            problems.append(f"no column found for {name}; headings read were: {seen}")

    if not any(name in columns for name in TAX_HEADS):
        problems.append(
            "no tax column found (cgst, sgst, igst or cess); a register with a taxable "
            "value and no tax heads cannot be reconciled against 2B"
        )

    return ColumnMap(
        columns=columns,
        ignored=ignored,
        unrecognised=unrecognised,
        problems=tuple(problems),
    )
