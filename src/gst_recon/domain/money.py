"""Money as Decimal, never float.

Tax arithmetic is compared against thresholds that carry legal consequences,
so binary floating point is not acceptable here: 0.1 + 0.2 != 0.3 is a bug
that would show up as a spurious mismatch on a rounding-sized difference,
which is precisely the class of difference this system exists to adjudicate.

All amounts are held to paise (2 dp) using ROUND_HALF_UP, the convention used
in Indian tax computation, rather than Python's default banker's rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISE = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value: str | int | float | Decimal) -> Decimal:
    """Coerce to a paise-quantised Decimal.

    float is accepted only so that untrusted spreadsheet input can be ingested;
    it is routed through str() first so we quantise the decimal literal the user
    saw rather than its binary approximation.
    """
    if isinstance(value, float):
        value = str(value)
    try:
        return Decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"cannot interpret {value!r} as an amount") from exc


@dataclass(frozen=True, slots=True)
class TaxAmounts:
    """The tax split on a single document.

    Intra-state supplies carry CGST+SGST; inter-state supplies carry IGST.
    Both are never populated together on a well-formed document, which is a
    cheap structural check on ingested data.
    """

    taxable: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO
    cess: Decimal = ZERO

    def __post_init__(self) -> None:
        for field_name in ("taxable", "cgst", "sgst", "igst", "cess"):
            object.__setattr__(self, field_name, money(getattr(self, field_name)))

    @property
    def total_tax(self) -> Decimal:
        return money(self.cgst + self.sgst + self.igst + self.cess)

    @property
    def invoice_value(self) -> Decimal:
        return money(self.taxable + self.total_tax)

    @property
    def is_interstate(self) -> bool:
        return self.igst > ZERO

    @property
    def well_formed_split(self) -> bool:
        """Interstate and intrastate tax heads must not both be populated."""
        intrastate = self.cgst > ZERO or self.sgst > ZERO
        return not (intrastate and self.igst > ZERO)

    def difference(self, other: TaxAmounts) -> Decimal:
        """Absolute difference in total tax -- the quantity tolerances apply to."""
        return abs(money(self.total_tax - other.total_tax))
