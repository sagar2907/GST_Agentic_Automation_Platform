"""GSTIN parsing and checksum validation.

A GSTIN is 15 characters: 2-digit state code, 10-character PAN, 1-character
entity number, a literal 'Z', and a check digit. The check digit uses a
base-36 weighted scheme defined by GSTN.

The house rule here is that we *flag* malformed identifiers and never silently
repair them. A transposed GSTIN that we "helpfully" corrected would attach a
tax credit to the wrong supplier, which is materially worse than refusing to
match and asking a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_VALUE = {c: i for i, c in enumerate(ALPHABET)}

# State code, PAN (5 alpha, 4 digit, 1 alpha), entity number, 'Z', check char.
GSTIN_RE = re.compile(r"^[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

GSTIN_LENGTH = 15
_CHECKSUM_BODY = 14


def compute_check_char(first_fourteen: str) -> str:
    """Return the GSTN check character for the first 14 characters of a GSTIN.

    Weights alternate 1, 2, 1, 2 ... starting at 1. Each product is reduced by
    adding its base-36 quotient and remainder -- the base-36 analogue of the
    "sum the digits" step in a Luhn checksum.
    """
    if len(first_fourteen) != _CHECKSUM_BODY:
        raise ValueError(f"expected {_CHECKSUM_BODY} characters, got {len(first_fourteen)}")
    total = 0
    for index, char in enumerate(first_fourteen):
        try:
            value = _VALUE[char]
        except KeyError:
            raise ValueError(f"character {char!r} is not valid in a GSTIN") from None
        factor = 1 if index % 2 == 0 else 2
        product = value * factor
        total += product // len(ALPHABET) + product % len(ALPHABET)
    return ALPHABET[(len(ALPHABET) - total % len(ALPHABET)) % len(ALPHABET)]


@dataclass(frozen=True, slots=True)
class GstinCheck:
    raw: str
    normalised: str
    well_formed: bool
    checksum_valid: bool
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.well_formed and self.checksum_valid

    @property
    def state_code(self) -> str | None:
        return self.normalised[:2] if self.well_formed else None


def normalise_gstin(raw: str) -> str:
    """Uppercase and strip all whitespace. Nothing else -- no character repair."""
    return re.sub(r"\s+", "", raw).upper()


def check_gstin(raw: str) -> GstinCheck:
    normalised = normalise_gstin(raw)
    if len(normalised) != GSTIN_LENGTH:
        return GstinCheck(
            raw, normalised, False, False, f"length {len(normalised)}, expected {GSTIN_LENGTH}"
        )
    if not GSTIN_RE.match(normalised):
        return GstinCheck(raw, normalised, False, False, "does not match GSTIN structure")
    expected = compute_check_char(normalised[:_CHECKSUM_BODY])
    if expected != normalised[14]:
        return GstinCheck(
            raw,
            normalised,
            True,
            False,
            f"check character {normalised[14]!r}, expected {expected!r}",
        )
    return GstinCheck(raw, normalised, True, True, None)


def is_valid_gstin(raw: str) -> bool:
    return check_gstin(raw).valid
