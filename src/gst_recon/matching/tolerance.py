"""Tax-difference tolerance, shared by the fuzzy pass and by classification.

Lives in its own module because both the matcher (deciding whether two lines
are the same document) and the classifier (reporting how far apart they are)
need it, and importing one from the other would be circular.
"""

from __future__ import annotations

from decimal import Decimal

from gst_recon.config import MatchTolerances
from gst_recon.domain.money import money
from gst_recon.domain.records import Gstr2bLine, PurchaseLine


def tax_within_tolerance(
    book: PurchaseLine, portal: Gstr2bLine, tolerances: MatchTolerances
) -> tuple[bool, Decimal]:
    """Is the tax difference inside the client's configured slack?

    Two allowances apply and the looser of them wins: a flat rupee amount for
    paise-level rounding, and a proportional band for larger invoices. The
    proportional band carries a hard cap so that a very large invoice cannot
    quietly absorb a very large absolute difference -- without the cap, 0.5% of
    a crore-rupee invoice would wave through a five-figure discrepancy.
    """
    delta = book.tax.difference(portal.tax)
    proportional = min(
        money(portal.tax.total_tax * tolerances.relative_tax_fraction),
        tolerances.absolute_tax_cap,
    )
    allowed = max(tolerances.absolute_tax_paise, proportional)
    return delta <= allowed, delta
