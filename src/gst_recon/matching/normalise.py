"""Normalisation: make two spellings of the same document comparable.

Normalisation is where most of the achievable matching accuracy lives, and
every point of accuracy here removes a case from Tier 2, which is the tier
that costs money. It is also the easiest place to do damage: a normaliser that
is too aggressive silently merges two genuinely different documents.

The rule followed throughout is *canonicalise formatting, never repair
content*. Punctuation and leading zeros are formatting. A wrong digit is
content, and content is escalated rather than fixed.
"""

from __future__ import annotations

import re
from datetime import date

from dateutil import parser as date_parser

_TOKEN_RE = re.compile(r"[A-Z]+|\d+")


def normalise_invoice_number(raw: str) -> str:
    """Reduce an invoice number to a canonical token sequence.

    ``INV/2026/001`` and ``INV-2026-1`` denote the same document and must
    compare equal; the separators are house style and the leading zeros are
    field width. Splitting into alphabetic and numeric runs and stripping
    leading zeros from the numeric ones achieves that without the collision
    risk of simply deleting every non-alphanumeric character, which would
    conflate ``INV/1/2026`` with ``INV/12/026``.
    """
    tokens = _TOKEN_RE.findall(raw.upper())
    canonical = [str(int(token)) if token.isdigit() else token for token in tokens]
    return "|".join(canonical)


def invoice_tokens(raw: str) -> tuple[str, ...]:
    """The canonical token sequence, before it is joined into a string."""
    return tuple(
        str(int(token)) if token.isdigit() else token for token in _TOKEN_RE.findall(raw.upper())
    )


def numbers_compatible(left: str, right: str) -> tuple[bool, str, float]:
    """Decide whether two invoice numbers denote the same document.

    A general string-similarity score is the wrong instrument here. Invoice
    numbers are overwhelmingly serial and share long prefixes by construction,
    so prefix-weighted measures such as Jaro-Winkler score ``INV/2026/2035``
    and ``INV/2026/1833`` at 0.85 -- above any threshold loose enough to catch
    real formatting variants. The digits those measures discount are precisely
    the digits that identify the document.

    The rule used instead: alphabetic tokens must agree exactly, and shared
    numeric tokens must be equal. One sequence may extend the other by a short
    house-style suffix. Every accepted match is therefore explainable in a
    sentence, which is the standard an auditor applies.

    Returns (compatible, reason, affinity) where affinity ranks quality.
    """
    left_tokens, right_tokens = invoice_tokens(left), invoice_tokens(right)
    if left_tokens == right_tokens:
        return True, "identical after normalisation", 1.0

    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    if longer[: len(shorter)] != shorter:
        return False, "token sequences diverge", 0.0
    extra = longer[len(shorter) :]
    if len(extra) > 1 or any(token.isdigit() for token in extra):
        # Extra digits change the document identity; extra letters are style.
        return False, f"extra identifying tokens {extra}", 0.0
    return True, f"house-style suffix {''.join(extra)!r}", 0.9


def parse_indian_date(raw: str | date) -> date:
    """Parse the day-first conventions used on Indian invoices into a date.

    ``dayfirst=True`` is load-bearing: 03/04/2026 is 3 April here, not 4 March.
    Reading it the American way shifts a document across a filing period and
    turns a clean match into a fabricated exception.
    """
    if isinstance(raw, date):
        return raw
    return date_parser.parse(raw, dayfirst=True).date()


def days_between(left: date, right: date) -> int:
    return abs((left - right).days)


def financial_year_start(day: date) -> int:
    """Return the calendar year in which the Indian financial year began.

    The Indian financial year runs 1 April to 31 March, so a January invoice
    belongs to the financial year that started the previous April.
    """
    return day.year if day.month >= 4 else day.year - 1
