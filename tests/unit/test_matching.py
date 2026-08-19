"""Tier 1 matching engine tests.

Several of these are regressions for wrong matches -- the failure mode that
matters most here. A missed match costs a human five minutes; a *wrong* match
silently attaches tax credit to the wrong document and is not visible to
anyone downstream.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date

import pytest

from gst_recon.config import MatchTolerances, PolicyThresholds
from gst_recon.data.generator import DISTRIBUTION_MIX, generate
from gst_recon.domain.money import TaxAmounts
from gst_recon.domain.records import Gstr2bLine, IrnStatus, PurchaseLine
from gst_recon.domain.taxonomy import ExceptionClass
from gst_recon.matching.engine import reconcile
from gst_recon.matching.normalise import (
    normalise_invoice_number,
    numbers_compatible,
    parse_indian_date,
)
from gst_recon.policy.gate import is_time_barred

BASE_DATE = date(2026, 7, 14)
TOLERANCES = MatchTolerances()
POLICY = PolicyThresholds()


def _reconcile(dataset):
    return reconcile(
        dataset.books, dataset.portal, tolerances=TOLERANCES, policy=POLICY, as_of=BASE_DATE
    )


def _full_dataset():
    return generate(
        seed=20260726,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=1800,
        injections=DISTRIBUTION_MIX,
        near_miss_pairs=120,
    )


# --- normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [("INV/2026/001", "INV-2026-1"), ("inv 2026 001", "INV/2026/1"), ("A/0007", "A-7")],
)
def test_formatting_variants_normalise_together(left: str, right: str) -> None:
    assert normalise_invoice_number(left) == normalise_invoice_number(right)


def test_normalisation_does_not_conflate_different_documents() -> None:
    """Deleting separators outright would merge these; token splitting must not."""
    assert normalise_invoice_number("INV/1/2026") != normalise_invoice_number("INV/12/026")


def test_normalisation_is_idempotent() -> None:
    once = normalise_invoice_number("INV/2026/0045")
    assert normalise_invoice_number(once) == once


def test_dates_are_read_day_first() -> None:
    """03/04/2026 is 3 April on an Indian invoice, not 4 March.

    Reading it month-first moves the document into a different filing period,
    which manufactures an exception out of a clean match.
    """
    assert parse_indian_date("03/04/2026") == date(2026, 4, 3)


# --- invoice number compatibility -----------------------------------------


def test_serial_numbers_sharing_a_prefix_are_not_compatible() -> None:
    """Regression: prefix-weighted string similarity produced a wrong match.

    With Jaro-Winkler at a 0.82 floor, ``INV02035`` and ``INV01833`` scored
    0.850 and were matched to each other. Their tax differed by Rs 59.78, which
    slipped under the proportional tolerance on a large invoice, so the engine
    paired two genuinely different documents and stranded both of their true
    counterparts. Invoice numbers are serial and share prefixes by
    construction, so the identifying digits are exactly what such measures
    discount.
    """
    compatible, _, _ = numbers_compatible("INV02035", "INV01833")
    assert not compatible


def test_house_style_suffix_is_compatible() -> None:
    compatible, reason, affinity = numbers_compatible("INV01833", "INV01833-A")
    assert compatible
    assert affinity < 1.0
    assert "suffix" in reason


def test_identical_numbers_rank_above_suffix_variants() -> None:
    _, _, exact_affinity = numbers_compatible("INV/2026/1", "INV-2026-001")
    _, _, suffix_affinity = numbers_compatible("INV01833", "INV01833-A")
    assert exact_affinity > suffix_affinity


def test_extra_numeric_token_is_not_a_suffix() -> None:
    """An extra number changes which document this is; an extra letter does not."""
    compatible, _, _ = numbers_compatible("INV/2026", "INV/2026/7")
    assert not compatible


# --- classification regressions -------------------------------------------


def test_duplicate_survives_its_twin_being_matched() -> None:
    """Regression: a duplicate was reported as MISSING_IN_BOOKS.

    The injector emits two portal lines and one book line. Exact matching
    consumed one portal line, and duplicate detection compared only the
    remaining *unmatched* lines against each other -- so the survivor had no
    sibling left to be a duplicate of and fell through to MISSING_IN_BOOKS.
    The index must be seeded with lines the earlier passes already consumed.
    """
    dataset = generate(
        seed=5,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=10,
        injections={ExceptionClass.DUPLICATE: 3},
    )
    found = Counter(e.exception_class for e in _reconcile(dataset).exceptions)
    assert found[ExceptionClass.DUPLICATE] == 3
    assert found[ExceptionClass.MISSING_IN_BOOKS] == 0


def test_reverse_charge_is_surfaced_even_though_it_matches_cleanly() -> None:
    """Regression: RCM documents vanished from the taxonomy.

    Reverse charge is a treatment flag, not a matching failure: both sides
    agree perfectly, so the pair matched in pass 1 and never reached
    classification. The class the taxonomy promises to surface silently
    disappeared. They must be lifted out before matching runs.
    """
    dataset = generate(
        seed=6,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=10,
        injections={ExceptionClass.RCM: 4},
    )
    found = Counter(e.exception_class for e in _reconcile(dataset).exceptions)
    assert found[ExceptionClass.RCM] == 4


def test_reverse_charge_pair_is_reported_once_not_twice() -> None:
    dataset = generate(
        seed=8,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=5,
        injections={ExceptionClass.RCM: 2},
    )
    rcm = [e for e in _reconcile(dataset).exceptions if e.exception_class is ExceptionClass.RCM]
    assert len(rcm) == 2
    assert all(e.book is not None and e.portal is not None for e in rcm)


def test_every_planted_class_is_recovered_exactly() -> None:
    dataset = _full_dataset()
    found = Counter(e.exception_class for e in _reconcile(dataset).exceptions)
    assert found == Counter(DISTRIBUTION_MIX)


# --- fuzzy pass ------------------------------------------------------------


def test_fuzzy_catches_every_planted_near_miss() -> None:
    dataset = _full_dataset()
    result = _reconcile(dataset)
    caught = {pair.book.line_id for pair in result.matched if not pair.exact}
    assert set(dataset.near_miss_keys) <= caught


def test_assignment_is_independent_of_input_order() -> None:
    """Regression: greedy assignment let the first book visited claim a line.

    Whichever book the iteration reached first took any acceptable portal line,
    even when a later book matched that line perfectly. Scoring all candidate
    pairs and assigning in quality order makes the outcome a property of the
    data rather than of dictionary ordering.
    """
    dataset = _full_dataset()
    baseline = _reconcile(dataset)
    baseline_pairs = {(p.book.line_id, p.portal.line_id) for p in baseline.matched}

    shuffler = random.Random(99)
    for _ in range(3):
        shuffler.shuffle(dataset.books)
        shuffler.shuffle(dataset.portal)
        assert {
            (p.book.line_id, p.portal.line_id) for p in _reconcile(dataset).matched
        } == baseline_pairs


def test_document_conservation() -> None:
    """Every input line ends up in exactly one place: matched, or an exception."""
    dataset = _full_dataset()
    result = _reconcile(dataset)
    accounted = 2 * len(result.matched)
    for exception in result.exceptions:
        accounted += (exception.book is not None) + (exception.portal is not None)
    assert accounted == len(dataset.books) + len(dataset.portal)


# --- statutory arithmetic --------------------------------------------------


@pytest.mark.parametrize(
    ("invoice_day", "as_of", "expected"),
    [
        (date(2024, 5, 12), date(2026, 7, 14), True),  # FY24-25, deadline 30 Nov 2025
        (date(2026, 5, 12), date(2026, 7, 14), False),  # FY26-27, still open
        (date(2025, 3, 31), date(2025, 11, 30), False),  # FY24-25, on the deadline
        (date(2025, 3, 31), date(2025, 12, 1), True),  # one day past it
    ],
)
def test_section_16_4_window(invoice_day: date, as_of: date, expected: bool) -> None:
    assert is_time_barred(invoice_day, as_of, POLICY) is expected


def test_tolerance_never_silently_absorbs_a_large_difference() -> None:
    """The proportional band is capped, so a big invoice cannot hide a big gap."""
    big = TaxAmounts(taxable="10000000.00", cgst="900000.00", sgst="900000.00")
    drifted = TaxAmounts(taxable="10000000.00", cgst="900500.00", sgst="900000.00")
    book = PurchaseLine("b", "27AAPFU0939F1ZV", "INV/1", BASE_DATE, big)
    portal = Gstr2bLine(
        "p", "27AAPFU0939F1ZV", "INV/1", BASE_DATE, drifted, irn_status=IrnStatus.ACTIVE
    )
    result = reconcile([book], [portal], tolerances=TOLERANCES, policy=POLICY, as_of=BASE_DATE)
    assert result.exceptions[0].exception_class is ExceptionClass.AMOUNT_MISMATCH
