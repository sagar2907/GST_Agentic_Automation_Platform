"""The Tier 1 matching engine: normalise, exact, fuzzy, classify.

Three passes, in a fixed order that is load-bearing. Exact matches are taken
first and set aside permanently, so a document that has an unambiguous
counterpart can never be consumed by a looser rule reaching for it. Fuzzy runs
only on what is left, and classification only on what neither pass claimed.
Reordering these passes changes the answers.

Blocking is by supplier GSTIN. Reconciliation is inherently per-supplier -- a
document from one vendor is never the same document as one from another -- so
blocking costs no recall and turns a quadratic comparison into a linear one.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from gst_recon.config import MatchTolerances, PolicyThresholds
from gst_recon.domain.gstin import normalise_gstin
from gst_recon.domain.records import (
    ExceptionRecord,
    Gstr2bLine,
    MatchedPair,
    MatchReason,
    PurchaseLine,
)
from gst_recon.domain.taxonomy import ExceptionClass
from gst_recon.matching.classify import (
    ExceptionSink,
    classify_book_residue,
    classify_portal_residue,
    classify_reverse_charge,
    key_of,
    pair_amount_mismatches,
    pair_gstin_mismatches,
)
from gst_recon.matching.normalise import days_between, numbers_compatible
from gst_recon.matching.tolerance import tax_within_tolerance

__all__ = ["ReconciliationResult", "reconcile", "tax_within_tolerance"]


@dataclass(slots=True)
class ReconciliationResult:
    matched: list[MatchedPair] = field(default_factory=list)
    exceptions: list[ExceptionRecord] = field(default_factory=list)
    exact_count: int = 0
    fuzzy_count: int = 0

    @property
    def total_documents(self) -> int:
        return len(self.matched) + len(self.exceptions)

    @property
    def exact_rate(self) -> float:
        return self.exact_count / self.total_documents if self.total_documents else 0.0

    @property
    def fuzzy_rate(self) -> float:
        return self.fuzzy_count / self.total_documents if self.total_documents else 0.0

    def by_class(self) -> dict[ExceptionClass, int]:
        counts: dict[ExceptionClass, int] = defaultdict(int)
        for exception in self.exceptions:
            counts[exception.exception_class] += 1
        return dict(counts)


def _match_exact(
    result: ReconciliationResult,
    unmatched_books: dict[str, PurchaseLine],
    unmatched_portal: dict[str, Gstr2bLine],
    portal: list[Gstr2bLine],
) -> None:
    portal_by_key: dict[tuple[str, str], list[Gstr2bLine]] = defaultdict(list)
    for line in portal:
        portal_by_key[key_of(line.supplier_gstin, line.invoice_number)].append(line)

    # Books and candidates are both walked in line-id order. When a document is
    # genuinely duplicated in 2B, two portal lines are equally valid partners
    # and the arbitrary winner would otherwise follow input ordering -- leaving
    # the audit trail naming a different line as "the duplicate" on each run.
    for book in sorted(unmatched_books.values(), key=lambda line: line.line_id):
        key = key_of(book.supplier_gstin, book.invoice_number)
        candidates = sorted(
            (line for line in portal_by_key.get(key, []) if line.line_id in unmatched_portal),
            key=lambda line: line.line_id,
        )
        for candidate in candidates:
            if book.tax.total_tax == candidate.tax.total_tax:
                reason = MatchReason(
                    "exact", "GSTIN, invoice number and tax agree exactly", Decimal("0.00")
                )
                result.matched.append(MatchedPair(book, candidate, reason, exact=True))
                result.exact_count += 1
                del unmatched_books[book.line_id]
                del unmatched_portal[candidate.line_id]
                break


def _match_fuzzy(
    result: ReconciliationResult,
    unmatched_books: dict[str, PurchaseLine],
    unmatched_portal: dict[str, Gstr2bLine],
    tolerances: MatchTolerances,
) -> None:
    """Score every acceptable pairing, then assign in quality order.

    Order-dependent greedy assignment produced a genuine wrong match: whichever
    book the iteration reached first claimed any acceptable portal line, even
    when a later book matched that line perfectly. Both true counterparts were
    stranded and reported as separate exceptions.
    """
    portal_by_gstin: dict[str, list[Gstr2bLine]] = defaultdict(list)
    for line in unmatched_portal.values():
        portal_by_gstin[normalise_gstin(line.supplier_gstin)].append(line)

    scored: list[tuple[float, Decimal, int, str, str, PurchaseLine, Gstr2bLine, MatchReason]] = []
    for book in unmatched_books.values():
        for candidate in portal_by_gstin.get(normalise_gstin(book.supplier_gstin), []):
            compatible, why, affinity = numbers_compatible(
                book.invoice_number, candidate.invoice_number
            )
            if not compatible:
                continue
            gap = days_between(book.invoice_date, candidate.invoice_date)
            if gap > tolerances.date_window_days:
                continue
            within, delta = tax_within_tolerance(book, candidate, tolerances)
            if not within:
                continue
            reason = MatchReason(
                rule="fuzzy",
                detail=f"{why}; dates {gap}d apart; tax differs by {delta}",
                tax_delta=delta,
                days_apart=gap,
                number_similarity=affinity,
            )
            # Sort key: best affinity, then smallest money gap, then closest
            # dates. Line ids break remaining ties so the result never depends
            # on dictionary iteration order.
            scored.append(
                (-affinity, delta, gap, book.line_id, candidate.line_id, book, candidate, reason)
            )

    for _, _, _, _, _, book, candidate, reason in sorted(scored, key=lambda row: row[:5]):
        if book.line_id not in unmatched_books or candidate.line_id not in unmatched_portal:
            continue
        result.matched.append(MatchedPair(book, candidate, reason, exact=False))
        result.fuzzy_count += 1
        del unmatched_books[book.line_id]
        del unmatched_portal[candidate.line_id]


def reconcile(
    books: list[PurchaseLine],
    portal: list[Gstr2bLine],
    *,
    tolerances: MatchTolerances,
    policy: PolicyThresholds,
    as_of: date,
) -> ReconciliationResult:
    result = ReconciliationResult()

    # Reverse charge is a treatment flag, not a matching failure. These
    # documents match their counterparts perfectly well, which is exactly why
    # they have to be lifted out *before* the matching passes: left in, they
    # pair up cleanly and never reach classification, so a class the taxonomy
    # promises to surface silently disappears.
    rcm_books = [line for line in books if line.reverse_charge]
    rcm_portal = [line for line in portal if line.reverse_charge]
    books = [line for line in books if not line.reverse_charge]
    portal = [line for line in portal if not line.reverse_charge]

    unmatched_books = {line.line_id: line for line in books}
    unmatched_portal = {line.line_id: line for line in portal}

    _match_exact(result, unmatched_books, unmatched_portal, portal)
    _match_fuzzy(result, unmatched_books, unmatched_portal, tolerances)

    # Pass 3. Paired classes are resolved before singleton classes, so a
    # document present on both sides is never reported twice under two labels.
    sink = ExceptionSink(result.exceptions)
    classify_reverse_charge(sink, rcm_books, rcm_portal)
    pair_amount_mismatches(sink, unmatched_books, unmatched_portal, tolerances)
    pair_gstin_mismatches(sink, unmatched_books, unmatched_portal)
    classify_portal_residue(sink, unmatched_portal, books, portal)
    classify_book_residue(sink, unmatched_books, as_of=as_of, policy=policy)
    return result
