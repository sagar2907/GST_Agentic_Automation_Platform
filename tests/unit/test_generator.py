"""Tests for the synthetic dataset generator.

The generator is load-bearing for every measured number in this project: if it
emits mislabelled ground truth, every accuracy figure downstream is wrong in a
way no amount of careful modelling will reveal. These tests exist to make that
failure mode loud.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date

import pytest

from gst_recon.data.generator import (
    DISTRIBUTION_MIX,
    HARD_MIX,
    Dataset,
    generate,
)
from gst_recon.domain.gstin import is_valid_gstin
from gst_recon.domain.taxonomy import ExceptionClass

BASE_DATE = date(2026, 7, 14)


def _fingerprint(dataset: Dataset) -> str:
    digest = hashlib.sha256()
    for line in dataset.books:
        digest.update(
            f"{line.line_id}{line.supplier_gstin}{line.invoice_number}"
            f"{line.invoice_date}{line.tax.total_tax}".encode()
        )
    for line in dataset.portal:
        digest.update(
            f"{line.line_id}{line.supplier_gstin}{line.invoice_number}"
            f"{line.invoice_date}{line.tax.total_tax}".encode()
        )
    return digest.hexdigest()


def _build(seed: int = 20260726, clean: int = 300, mix=None) -> Dataset:
    return generate(
        seed=seed,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=clean,
        injections=mix if mix is not None else DISTRIBUTION_MIX,
    )


def test_same_seed_reproduces_identical_dataset() -> None:
    assert _fingerprint(_build()) == _fingerprint(_build())


def test_different_seed_changes_dataset() -> None:
    assert _fingerprint(_build(seed=1)) != _fingerprint(_build(seed=2))


def test_truth_labels_match_the_injection_spec_exactly() -> None:
    dataset = _build()
    planted = Counter(label.exception_class for label in dataset.truth)
    assert planted == Counter(DISTRIBUTION_MIX)


def test_portal_and_vendor_gstins_are_checksum_valid() -> None:
    """Only deliberately corrupted book entries may fail the checksum."""
    dataset = _build()
    authoritative = {line.supplier_gstin for line in dataset.portal}
    authoritative |= {vendor.gstin for vendor in dataset.vendors.values()}
    assert [g for g in authoritative if not is_valid_gstin(g)] == []


def test_transposed_gstin_always_differs_from_the_authoritative_one() -> None:
    """Regression: a transposition of two *identical* characters is a no-op.

    Seed 20260726 previously produced case X1937, whose vendor GSTIN contained
    the run "LLL". The injector swapped two of those L characters, leaving the
    book GSTIN byte-identical to the portal GSTIN while still labelling the
    case GSTIN_MISMATCH. Tier 1 matched it cleanly, so the case scored as a
    false negative against ground truth that was itself wrong.

    The fix restricts the swap to index pairs whose characters differ. This
    test sweeps many seeds because the bug only appeared for vendors that
    happened to carry a repeated character in the swap window.
    """
    for seed in range(40):
        dataset = generate(
            seed=seed,
            period="07-2026",
            base_date=BASE_DATE,
            clean_pairs=40,
            injections={ExceptionClass.GSTIN_MISMATCH: 12},
        )
        books = {line.line_id: line for line in dataset.books}
        portal = {line.line_id: line for line in dataset.portal}
        for label in dataset.truth:
            book = books[label.exception_key]
            counterpart = portal[label.exception_key + "P"]
            assert book.supplier_gstin != counterpart.supplier_gstin, (
                f"seed {seed} case {label.exception_key}: transposition was a no-op"
            )


def test_hard_mix_contains_only_tier_two_classes() -> None:
    """The hard set exists to feed the agent, so it must not carry rule-closable cases."""
    rule_closable = {
        ExceptionClass.DUPLICATE,
        ExceptionClass.CANCELLED_IRN,
        ExceptionClass.RCM,
        ExceptionClass.TIME_BARRED,
        ExceptionClass.MISSING_IN_2B,
    }
    assert set(HARD_MIX) & rule_closable == set()


def test_no_wall_clock_dependency() -> None:
    """Regenerating with the same base date must not depend on today's date."""
    first = generate(
        seed=7,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=50,
        injections={ExceptionClass.MISSING_IN_2B: 5},
    )
    second = generate(
        seed=7,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=50,
        injections={ExceptionClass.MISSING_IN_2B: 5},
    )
    assert _fingerprint(first) == _fingerprint(second)
    assert all(line.invoice_date <= BASE_DATE for line in first.books)


@pytest.mark.parametrize("exception_class", list(ExceptionClass))
def test_every_class_can_be_injected_alone(exception_class: ExceptionClass) -> None:
    dataset = generate(
        seed=11,
        period="07-2026",
        base_date=BASE_DATE,
        clean_pairs=25,
        injections={exception_class: 3},
    )
    assert len(dataset.truth) == 3
    assert {label.exception_class for label in dataset.truth} == {exception_class}
