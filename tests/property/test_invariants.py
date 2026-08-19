"""Property tests for invariants the matching engine must never violate.

Example-based tests check the cases I thought of. These check the cases I did
not: normalisation that is not idempotent, money that does not round-trip,
documents that vanish between passes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from gst_recon.config import MatchTolerances, PolicyThresholds
from gst_recon.domain.gstin import compute_check_char, is_valid_gstin, normalise_gstin
from gst_recon.domain.money import TaxAmounts, money
from gst_recon.domain.records import Gstr2bLine, PurchaseLine
from gst_recon.matching.engine import reconcile, tax_within_tolerance
from gst_recon.matching.normalise import (
    invoice_tokens,
    normalise_invoice_number,
    numbers_compatible,
)

TOLERANCES = MatchTolerances()
POLICY = PolicyThresholds()
BASE_DATE = date(2026, 7, 14)

invoice_numbers = st.text(
    alphabet=st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-_ "),
    min_size=1,
    max_size=24,
)
amounts = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("10000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


@given(invoice_numbers)
def test_normalisation_is_idempotent(raw: str) -> None:
    once = normalise_invoice_number(raw)
    assert normalise_invoice_number(once) == once


@given(invoice_numbers)
def test_normalisation_never_invents_tokens(raw: str) -> None:
    assert len(invoice_tokens(normalise_invoice_number(raw))) == len(invoice_tokens(raw))


@given(invoice_numbers, invoice_numbers)
def test_number_compatibility_is_symmetric(left: str, right: str) -> None:
    """Whether two documents are the same cannot depend on argument order."""
    assert numbers_compatible(left, right)[0] == numbers_compatible(right, left)[0]


@given(invoice_numbers)
def test_number_compatibility_is_reflexive(raw: str) -> None:
    assume(invoice_tokens(raw))
    assert numbers_compatible(raw, raw)[0]


@given(amounts)
def test_money_round_trips_through_string(value: Decimal) -> None:
    assert money(str(money(value))) == money(value)


@given(amounts, amounts, amounts)
def test_total_tax_is_the_sum_of_its_heads(cgst: Decimal, sgst: Decimal, cess: Decimal) -> None:
    tax = TaxAmounts(taxable="0.00", cgst=cgst, sgst=sgst, cess=cess)
    assert tax.total_tax == money(money(cgst) + money(sgst) + money(cess))


@given(amounts, amounts)
def test_tax_difference_is_symmetric(left: Decimal, right: Decimal) -> None:
    a = TaxAmounts(taxable="0.00", cgst=left)
    b = TaxAmounts(taxable="0.00", cgst=right)
    assert a.difference(b) == b.difference(a)


@given(amounts)
def test_a_document_always_matches_itself_within_tolerance(cgst: Decimal) -> None:
    tax = TaxAmounts(taxable="1000.00", cgst=cgst)
    book = PurchaseLine("b", "27AAPFU0939F1ZV", "INV/1", BASE_DATE, tax)
    portal = Gstr2bLine("p", "27AAPFU0939F1ZV", "INV/1", BASE_DATE, tax)
    within, delta = tax_within_tolerance(book, portal, TOLERANCES)
    assert within
    assert delta == Decimal("0.00")


@given(st.lists(st.tuples(invoice_numbers, amounts), min_size=0, max_size=12))
@settings(max_examples=60, deadline=None)
def test_documents_are_conserved_across_reconciliation(rows) -> None:
    """No input line may be dropped or double-counted by the three passes."""
    gstin = "27AAPFU0939F1ZV"
    books = [
        PurchaseLine(
            f"B{i}", gstin, number or "X", BASE_DATE, TaxAmounts(taxable="1000.00", cgst=value)
        )
        for i, (number, value) in enumerate(rows)
    ]
    portal = [
        Gstr2bLine(
            f"P{i}", gstin, number or "X", BASE_DATE, TaxAmounts(taxable="1000.00", cgst=value)
        )
        for i, (number, value) in enumerate(rows)
    ]
    result = reconcile(books, portal, tolerances=TOLERANCES, policy=POLICY, as_of=BASE_DATE)
    accounted = 2 * len(result.matched)
    for exception in result.exceptions:
        accounted += (exception.book is not None) + (exception.portal is not None)
    assert accounted == len(books) + len(portal)


@given(
    st.sampled_from(["27", "29", "07", "19", "24", "33"]),
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=5, max_size=5),
    st.integers(min_value=0, max_value=9999),
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=1),
    st.sampled_from("123456789"),
)
def test_computed_check_character_always_validates(
    state: str, letters: str, digits: int, tail: str, entity: str
) -> None:
    """The checksum must accept anything it generated -- round-trip closure.

    This replaces fixture GSTINs entirely. Plausible-looking example GSTINs
    generally fail their own checksum, so asserting against invented constants
    tests the fixture rather than the algorithm.
    """
    body = f"{state}{letters}{digits:04d}{tail}{entity}Z"
    assert is_valid_gstin(body + compute_check_char(body))


@given(
    st.text(alphabet=" \t\n", min_size=0, max_size=4),
    st.text(alphabet=" \t\n", min_size=0, max_size=4),
)
def test_gstin_normalisation_strips_surrounding_whitespace(lead: str, trail: str) -> None:
    body = "27AAPFU0939F1Z"
    gstin = body + compute_check_char(body)
    assert normalise_gstin(f"{lead}{gstin}{trail}") == gstin
