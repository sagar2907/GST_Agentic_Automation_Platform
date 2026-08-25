"""Turning spreadsheet text into values, and refusing when it cannot be done.

The rule this module is built around: **an ingest step that guesses is worse
than one that stops.** A misread amount produces a mismatch someone eventually
investigates. A misread date produces a document that looks in-period when it
is time-barred, or the reverse, and nothing downstream ever questions it
because the value is perfectly well-formed. Silence is the failure mode worth
engineering against.

Dates are where that bites hardest. ``03/04/2026`` is 3 April to an Indian
accountant and 4 March to an American spreadsheet, and both readings are
plausible for a file whose provenance nobody recorded. A single value cannot
be resolved. A *column* usually can: one row with a day past the 12th settles
the whole column's convention. When no row settles it, this refuses rather
than falling back to a locale default -- the case where the guess is unfalsifiable
is exactly the case where it must not be made silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from gst_recon.domain.money import money

# Currency decoration a register may carry. Stripped before parsing, never
# interpreted: "INR" tells us nothing this system does not already assume.
_CURRENCY = re.compile(r"[₹$]|\b(?:INR|RS|RS\.)\b", re.IGNORECASE)
_SEPARATORS = re.compile(r"[,\s]")
_NUMERIC = re.compile(r"^-?\d*\.?\d+$")
_DMY = re.compile(r"^(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


class CoercionError(ValueError):
    """A cell could not be read as the type its column requires."""


def parse_amount(text: str) -> Decimal:
    """Read a currency cell.

    Accounting negatives -- ``(1,234.56)`` -- are honoured, because a credit
    shown in parentheses that parsed as positive would flip the sign on exactly
    the documents that reduce a claim.
    """
    cleaned = _CURRENCY.sub("", text).strip()
    if not cleaned:
        raise CoercionError("empty amount")

    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative, cleaned = True, cleaned[1:-1].strip()
    # Some exports trail the sign rather than lead it.
    if cleaned.endswith("-"):
        negative, cleaned = True, cleaned[:-1].strip()
    # Tally writes Dr/Cr suffixes. They describe the ledger side, not the sign
    # of the figure in this column, so they are dropped rather than applied.
    cleaned = re.sub(r"\b(DR|CR)\b\.?$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = _SEPARATORS.sub("", cleaned)

    if not _NUMERIC.match(cleaned):
        raise CoercionError(f"{text!r} is not an amount")
    try:
        value = money(cleaned)
    except ValueError as exc:
        raise CoercionError(f"{text!r} is not an amount") from exc
    return -value if negative else value


class DateOrder(StrEnum):
    """Which of the first two components of a numeric date is the day."""

    DAY_FIRST = "DAY_FIRST"
    MONTH_FIRST = "MONTH_FIRST"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class DateColumn:
    """A date column's resolved reading, and how it was resolved.

    ``evidence`` names the value that settled it, so a reviewer who disagrees
    with the interpretation can go and look at that row.
    """

    order: DateOrder
    evidence: str

    @property
    def resolved(self) -> bool:
        return self.order is not DateOrder.UNRESOLVED


def _components(text: str) -> tuple[int, int, int] | None:
    match = _DMY.match(text.strip())
    if match is None:
        return None
    first, second, year = (int(part) for part in match.groups())
    if year < 100:
        # A two-digit year in a tax document is this century. 1926 is not a
        # filing period anyone will reconcile.
        year += 2000
    return first, second, year


def infer_date_order(values: list[str]) -> DateColumn:
    """Decide whether a column is day-first or month-first, from the column.

    Unambiguously-ISO values are ignored here: they carry no evidence about how
    the *numeric* values in the same column should be read.
    """
    day_first_evidence = ""
    month_first_evidence = ""
    for text in values:
        parts = _components(text)
        if parts is None:
            continue
        first, second, _ = parts
        if first > 12 and not day_first_evidence:
            day_first_evidence = text
        if second > 12 and not month_first_evidence:
            month_first_evidence = text

    if day_first_evidence and month_first_evidence:
        # Both readings are contradicted by some row, so the column is not
        # written in one convention at all. Nothing here can be trusted.
        return DateColumn(
            DateOrder.UNRESOLVED,
            f"{day_first_evidence} needs day-first, {month_first_evidence} needs month-first",
        )
    if day_first_evidence:
        return DateColumn(DateOrder.DAY_FIRST, day_first_evidence)
    if month_first_evidence:
        return DateColumn(DateOrder.MONTH_FIRST, month_first_evidence)
    return DateColumn(DateOrder.UNRESOLVED, "")


def parse_date(text: str, order: DateOrder) -> date:
    """Read a date cell under an already-decided column convention."""
    stripped = text.strip()
    iso = _ISO.match(stripped)
    if iso is not None:
        # Unambiguous by construction, so the column's convention is irrelevant.
        year, month, day = (int(part) for part in iso.groups())
        return _build(year, month, day, stripped)

    parts = _components(stripped)
    if parts is None:
        raise CoercionError(f"{text!r} is not a date this reader recognises")
    first, second, year = parts

    if order is DateOrder.DAY_FIRST:
        day, month = first, second
    elif order is DateOrder.MONTH_FIRST:
        day, month = second, first
    else:
        raise CoercionError(
            f"{text!r} is ambiguous and nothing in its column resolves it; "
            "state the date order explicitly rather than have one guessed"
        )
    return _build(year, month, day, stripped)


def _build(year: int, month: int, day: int, original: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise CoercionError(f"{original!r} is not a real date: {exc}") from exc


def parse_flag(text: str) -> bool:
    """Read a yes/no column.

    Blank is False. Anything neither affirmative nor negative raises, because a
    reverse-charge column that quietly reads ``UNKNOWN`` as ``No`` moves the
    tax liability to the wrong party.
    """
    value = text.strip().lower()
    if value in {"", "n", "no", "false", "0", "f"}:
        return False
    if value in {"y", "yes", "true", "1", "t"}:
        return True
    raise CoercionError(f"{text!r} is neither yes nor no")


def normalise_identifier(text: str) -> str:
    """Uppercase and strip an identifier without altering its characters.

    Deliberately not a repair. Spacing and case are presentation; anything else
    is data, and a GSTIN or invoice number this system "fixed" would attach
    credit to a document the taxpayer never has.
    """
    return _SEPARATORS.sub("", text).strip().upper()


def parse_decimal_or_zero(text: str) -> Decimal:
    """Amount columns that are legitimately blank, such as an unused tax head."""
    if not text.strip():
        return money("0")
    try:
        return parse_amount(text)
    except (CoercionError, InvalidOperation) as exc:
        raise CoercionError(str(exc)) from exc
