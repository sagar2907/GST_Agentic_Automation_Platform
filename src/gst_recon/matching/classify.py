"""Pass 3: apply the exception taxonomy to whatever matching left behind.

Split out of the engine because classification is five separate jobs wearing
one name, and they have different failure modes. Paired classes -- where both
sides hold the document and only disagree about it -- must be resolved before
singleton classes, or the same document is reported twice under two labels.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from gst_recon.config import MatchTolerances, PolicyThresholds
from gst_recon.domain.gstin import is_valid_gstin, normalise_gstin
from gst_recon.domain.records import (
    DocumentKind,
    ExceptionRecord,
    Gstr2bLine,
    IrnStatus,
    PurchaseLine,
)
from gst_recon.domain.taxonomy import ExceptionClass
from gst_recon.matching.normalise import normalise_invoice_number
from gst_recon.matching.tolerance import tax_within_tolerance
from gst_recon.policy.gate import is_time_barred


def key_of(gstin: str, number: str) -> tuple[str, str]:
    return normalise_gstin(gstin), normalise_invoice_number(number)


@dataclass(slots=True)
class ExceptionSink:
    """Collects exceptions and assigns their stable identifiers.

    Sequence numbers are handed out here rather than by each classifier, so an
    exception id encodes the order in which the taxonomy was applied. That is
    what makes two runs over the same input directly diffable.
    """

    exceptions: list[ExceptionRecord]
    counter: int = 0

    def emit(
        self,
        cls: ExceptionClass,
        *,
        detail: str,
        book: PurchaseLine | None = None,
        portal: Gstr2bLine | None = None,
        candidates: tuple[str, ...] = (),
    ) -> None:
        anchor = portal.line_id if portal is not None else book.line_id if book else "?"
        self.counter += 1
        self.exceptions.append(
            ExceptionRecord(
                f"E{self.counter:05d}-{anchor}",
                cls,
                book=book,
                portal=portal,
                detail=detail,
                candidates=candidates,
            )
        )


def classify_reverse_charge(
    sink: ExceptionSink,
    rcm_books: list[PurchaseLine],
    rcm_portal: list[Gstr2bLine],
) -> None:
    """Emit reverse-charge documents, pairing the two sides where both exist."""
    portal_by_key = {key_of(line.supplier_gstin, line.invoice_number): line for line in rcm_portal}
    paired: set[str] = set()
    for line in sorted(rcm_books, key=lambda entry: entry.line_id):
        counterpart = portal_by_key.get(key_of(line.supplier_gstin, line.invoice_number))
        if counterpart is not None:
            paired.add(counterpart.line_id)
        sink.emit(
            ExceptionClass.RCM,
            book=line,
            portal=counterpart,
            detail="reverse charge supply; credit follows separate treatment",
        )
    for line in sorted(rcm_portal, key=lambda entry: entry.line_id):
        if line.line_id not in paired:
            sink.emit(
                ExceptionClass.RCM,
                portal=line,
                detail="reverse charge supply reported by the supplier only",
            )


def pair_amount_mismatches(
    sink: ExceptionSink,
    unmatched_books: dict[str, PurchaseLine],
    unmatched_portal: dict[str, Gstr2bLine],
    tolerances: MatchTolerances,
) -> None:
    """Pair the two sides of a document they both hold but disagree about.

    These survived matching precisely because they disagree, so reporting them
    as two unrelated singletons would hide the fact that the document is
    present on both sides and only the money is in dispute.
    """
    portal_by_key: dict[tuple[str, str], list[Gstr2bLine]] = defaultdict(list)
    for line in unmatched_portal.values():
        portal_by_key[key_of(line.supplier_gstin, line.invoice_number)].append(line)

    for book in sorted(unmatched_books.values(), key=lambda line: line.line_id):
        key = key_of(book.supplier_gstin, book.invoice_number)
        for candidate in sorted(portal_by_key.get(key, []), key=lambda line: line.line_id):
            if candidate.line_id not in unmatched_portal:
                continue
            _, delta = tax_within_tolerance(book, candidate, tolerances)
            sink.emit(
                ExceptionClass.AMOUNT_MISMATCH,
                book=book,
                portal=candidate,
                detail=f"tax differs by {delta}, beyond configured tolerance",
            )
            del unmatched_books[book.line_id]
            del unmatched_portal[candidate.line_id]
            break


def pair_gstin_mismatches(
    sink: ExceptionSink,
    unmatched_books: dict[str, PurchaseLine],
    unmatched_portal: dict[str, Gstr2bLine],
) -> None:
    """Treat a checksum-failing book GSTIN as a transcription error.

    A book entry whose GSTIN fails its own checksum, sitting against an
    otherwise identical portal entry, is a mistyped identifier rather than a
    previously unknown vendor.
    """
    portal_by_number: dict[str, list[Gstr2bLine]] = defaultdict(list)
    for line in unmatched_portal.values():
        portal_by_number[normalise_invoice_number(line.invoice_number)].append(line)

    for book in sorted(unmatched_books.values(), key=lambda line: line.line_id):
        if is_valid_gstin(book.supplier_gstin):
            continue
        number = normalise_invoice_number(book.invoice_number)
        candidates = sorted(portal_by_number.get(number, []), key=lambda line: line.line_id)
        for candidate in candidates:
            if candidate.line_id not in unmatched_portal:
                continue
            if book.tax.total_tax != candidate.tax.total_tax:
                continue
            sink.emit(
                ExceptionClass.GSTIN_MISMATCH,
                book=book,
                portal=candidate,
                detail=(
                    f"book GSTIN {book.supplier_gstin} fails checksum; portal "
                    f"reports {candidate.supplier_gstin} for the same document"
                ),
                candidates=(candidate.line_id,),
            )
            del unmatched_books[book.line_id]
            del unmatched_portal[candidate.line_id]
            break


def classify_portal_residue(
    sink: ExceptionSink,
    unmatched_portal: dict[str, Gstr2bLine],
    all_books: list[PurchaseLine],
    all_portal: list[Gstr2bLine],
) -> None:
    known_numbers = {normalise_invoice_number(line.invoice_number) for line in all_portal}
    known_numbers |= {normalise_invoice_number(line.invoice_number) for line in all_books}

    # Seed the duplicate index with portal lines the earlier passes already
    # consumed. A duplicate is precisely a document whose twin matched the book
    # entry -- comparing only the leftovers against each other means the
    # survivor has nothing to be a duplicate *of*, and it is misreported as
    # missing from the books instead.
    seen: dict[tuple[str, str], str] = {
        key_of(line.supplier_gstin, line.invoice_number): line.line_id
        for line in all_portal
        if line.line_id not in unmatched_portal
    }

    for line in sorted(unmatched_portal.values(), key=lambda entry: entry.line_id):
        if line.irn_status is IrnStatus.CANCELLED:
            sink.emit(
                ExceptionClass.CANCELLED_IRN,
                portal=line,
                detail=f"IRN {line.irn} is cancelled at the portal but present in 2B",
            )
            continue
        key = key_of(line.supplier_gstin, line.invoice_number)
        if key in seen:
            sink.emit(
                ExceptionClass.DUPLICATE,
                portal=line,
                detail=f"same supplier and invoice number as {seen[key]}, reported twice",
                candidates=(seen[key],),
            )
            continue
        seen[key] = line.line_id
        original = line.original_invoice_number
        if line.kind is DocumentKind.CREDIT_NOTE and (
            original is None or normalise_invoice_number(original) not in known_numbers
        ):
            sink.emit(
                ExceptionClass.CREDIT_NOTE_UNLINKED,
                portal=line,
                detail=f"credit note references {original!r}, absent from this period",
            )
            continue
        sink.emit(
            ExceptionClass.MISSING_IN_BOOKS,
            portal=line,
            detail="present in GSTR-2B with no counterpart in the purchase register",
        )


def classify_book_residue(
    sink: ExceptionSink,
    unmatched_books: dict[str, PurchaseLine],
    *,
    as_of: date,
    policy: PolicyThresholds,
) -> None:
    for line in sorted(unmatched_books.values(), key=lambda entry: entry.line_id):
        if is_time_barred(line.invoice_date, as_of, policy):
            sink.emit(
                ExceptionClass.TIME_BARRED,
                book=line,
                detail=(
                    f"invoice dated {line.invoice_date} is outside the "
                    "Section 16(4) window; report only"
                ),
            )
            continue
        sink.emit(
            ExceptionClass.MISSING_IN_2B,
            book=line,
            detail="in the purchase register with no counterpart in GSTR-2B",
        )
