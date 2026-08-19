"""Deterministic synthetic dataset generation.

Everything here is driven by a single seeded ``random.Random``. There are no
clock reads anywhere in this module: the filing calendar is derived from an
explicit base period passed in by the caller. That matters because the whole
evaluation rests on being able to regenerate byte-identical inputs months
later and get the same numbers.

The generator emits ground truth alongside the data. That is the only reason
the tiering ablation can be scored at all -- without a label set, "the agent
resolved it" is an unfalsifiable claim.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from gst_recon.domain.gstin import compute_check_char
from gst_recon.domain.money import TaxAmounts, money
from gst_recon.domain.records import DocumentKind, Gstr2bLine, IrnStatus, PurchaseLine
from gst_recon.domain.taxonomy import ExceptionClass

STATE_CODES = ("27", "29", "07", "19", "24", "33", "06", "36")
PAN_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GST_RATES = (Decimal("0.05"), Decimal("0.12"), Decimal("0.18"), Decimal("0.28"))

VENDOR_STEMS = (
    "Sundaram",
    "Mahalakshmi",
    "Deccan",
    "Konark",
    "Vindhya",
    "Trishul",
    "Anand",
    "Pragati",
    "Suvarna",
    "Girnar",
    "Chettinad",
    "Kaveri",
    "Bharat",
    "Sahyadri",
    "Nilgiri",
    "Ratnagiri",
    "Malabar",
    "Aravalli",
)
VENDOR_SUFFIXES = (
    "Traders",
    "Industries",
    "Enterprises",
    "Agencies",
    "Polymers",
    "Textiles",
    "Logistics",
    "Steel Works",
)


def _make_gstin(rng: random.Random) -> str:
    """Build a structurally valid GSTIN with a correct check character.

    Computing the check digit rather than hardcoding fixtures is deliberate:
    invented "example" GSTINs generally fail their own checksum, so a test
    suite built on them would assert the wrong thing.
    """
    state = rng.choice(STATE_CODES)
    pan = (
        "".join(rng.choice(PAN_LETTERS) for _ in range(5))
        + f"{rng.randrange(10000):04d}"
        + rng.choice(PAN_LETTERS)
    )
    entity = rng.choice("123456789")
    body = f"{state}{pan}{entity}Z"
    return body + compute_check_char(body)


@dataclass(frozen=True, slots=True)
class Vendor:
    gstin: str
    legal_name: str
    files_late: bool
    risk_flag: str | None
    invoice_style: str


def _build_vendors(rng: random.Random, count: int) -> dict[str, Vendor]:
    """Build the vendor master.

    Roughly a third of vendors are marked as late filers, which is what drives
    the MISSING_IN_2B population -- the dominant exception class in practice,
    because your credit depends on somebody else's paperwork arriving on time.
    """
    vendors: dict[str, Vendor] = {}
    for _ in range(count):
        gstin = _make_gstin(rng)
        vendors[gstin] = Vendor(
            gstin=gstin,
            legal_name=f"{rng.choice(VENDOR_STEMS)} {rng.choice(VENDOR_SUFFIXES)}",
            files_late=rng.random() < 0.30,
            risk_flag=None if rng.random() > 0.08 else "REPEATED_LATE_FILING",
            invoice_style=rng.choice(("slash", "dash", "plain", "compact")),
        )
    return vendors


@dataclass(frozen=True, slots=True)
class BankPayment:
    payment_id: str
    counterparty_gstin: str
    amount: Decimal
    value_date: date
    narration: str


@dataclass(frozen=True, slots=True)
class TruthLabel:
    """Ground truth for one injected exception."""

    exception_key: str
    exception_class: ExceptionClass
    resolvable: bool
    resolution_note: str
    probe_that_resolves: str | None


@dataclass(slots=True)
class Dataset:
    period: str
    books: list[PurchaseLine]
    portal: list[Gstr2bLine]
    vendors: dict[str, Vendor]
    bank: list[BankPayment]
    irn_registry: dict[str, IrnStatus]
    prior_portal: dict[str, list[Gstr2bLine]] = field(default_factory=dict)
    truth: list[TruthLabel] = field(default_factory=list)
    # Pairs that exact matching must miss and fuzzy matching must catch. Held
    # separately from truth because they are matches, not exceptions -- they
    # exist to make the fuzzy pass measurable rather than merely present.
    near_miss_keys: list[str] = field(default_factory=list)

    @property
    def truth_by_key(self) -> dict[str, TruthLabel]:
        return {t.exception_key: t for t in self.truth}


def _format_number(style: str, serial: int, fy: str) -> str:
    if style == "slash":
        return f"INV/{fy}/{serial:04d}"
    if style == "dash":
        return f"INV-{fy}-{serial}"
    if style == "plain":
        return f"{fy}{serial:05d}"
    return f"INV{serial:05d}"


def _tax_for(rng: random.Random, taxable: Decimal, interstate: bool) -> TaxAmounts:
    rate = rng.choice(GST_RATES)
    total = money(taxable * rate)
    if interstate:
        return TaxAmounts(taxable=taxable, igst=total)
    half = money(total / 2)
    # Split halves must re-sum to the total; give the remainder to CGST so the
    # pair is never off by a paisa, which would otherwise look like a mismatch.
    return TaxAmounts(taxable=taxable, cgst=money(total - half), sgst=half)


def generate(
    *,
    seed: int,
    period: str,
    base_date: date,
    clean_pairs: int,
    injections: dict[ExceptionClass, int],
    vendor_count: int = 40,
    near_miss_pairs: int = 0,
) -> Dataset:
    """Generate one filing cycle.

    ``injections`` gives the exact number of each exception class to plant, so
    a caller can build either a realistic distribution or a residue-heavy hard
    set from the same code path.
    """
    rng = random.Random(seed)
    fy = str(base_date.year)

    vendors = _build_vendors(rng, vendor_count)
    vendor_list = list(vendors.values())

    books: list[PurchaseLine] = []
    portal: list[Gstr2bLine] = []
    bank: list[BankPayment] = []
    irn_registry: dict[str, IrnStatus] = {}
    truth: list[TruthLabel] = []
    serial = 1

    def next_doc(vendor: Vendor) -> tuple[str, date, TaxAmounts, str]:
        nonlocal serial
        number = _format_number(vendor.invoice_style, serial, fy)
        doc_date = base_date - timedelta(days=rng.randrange(1, 28))
        taxable = money(Decimal(rng.randrange(5_000, 900_000)))
        interstate = vendor.gstin[:2] != "27"
        irn = f"{serial:06d}{rng.randrange(10**10, 10**11)}"
        serial += 1
        return number, doc_date, _tax_for(rng, taxable, interstate), irn

    # --- clean, exactly-matching pairs -------------------------------------
    for _ in range(clean_pairs):
        vendor = rng.choice(vendor_list)
        number, doc_date, tax, irn = next_doc(vendor)
        irn_registry[irn] = IrnStatus.ACTIVE
        books.append(PurchaseLine(f"B{serial}", vendor.gstin, number, doc_date, tax, irn=irn))
        portal.append(
            Gstr2bLine(
                f"P{serial}",
                vendor.gstin,
                number,
                doc_date,
                tax,
                irn=irn,
                irn_status=IrnStatus.ACTIVE,
                return_period=period,
            )
        )
        if rng.random() < 0.8:
            bank.append(
                BankPayment(
                    f"BP{serial}",
                    vendor.gstin,
                    tax.invoice_value,
                    doc_date + timedelta(days=rng.randrange(5, 45)),
                    f"NEFT {vendor.legal_name}",
                )
            )

    # --- near misses: exact must fail, fuzzy must succeed ------------------
    near_miss_keys: list[str] = []
    for index in range(near_miss_pairs):
        vendor = rng.choice(vendor_list)
        number, doc_date, tax, irn = next_doc(vendor)
        key = f"N{serial}"
        near_miss_keys.append(key)
        if index % 2 == 0:
            # Paise-level rounding: identical document, tax off by under a rupee.
            drift = money(Decimal(rng.randrange(1, 90)) / 100)
            portal_tax = TaxAmounts(
                taxable=tax.taxable, cgst=money(tax.cgst + drift), sgst=tax.sgst, igst=tax.igst
            )
            portal_number, portal_date = number, doc_date
        else:
            # House-style suffix plus a few days' drift in the recorded date.
            portal_tax = tax
            portal_number = f"{number}-A"
            portal_date = doc_date + timedelta(days=rng.randrange(1, 6))
        books.append(PurchaseLine(key, vendor.gstin, number, doc_date, tax, irn=irn))
        portal.append(
            Gstr2bLine(
                f"{key}P",
                vendor.gstin,
                portal_number,
                portal_date,
                portal_tax,
                irn=irn,
                irn_status=IrnStatus.ACTIVE,
                return_period=period,
            )
        )
        irn_registry[irn] = IrnStatus.ACTIVE

    def plant(cls: ExceptionClass, count: int) -> None:
        for _ in range(count):
            vendor = rng.choice(vendor_list)
            number, doc_date, tax, irn = next_doc(vendor)
            key = f"X{serial}"
            _INJECTORS[cls](
                _InjectionContext(
                    rng,
                    key,
                    vendor,
                    number,
                    doc_date,
                    tax,
                    irn,
                    period,
                    base_date,
                    books,
                    portal,
                    bank,
                    irn_registry,
                    truth,
                )
            )

    for cls, count in injections.items():
        plant(cls, count)

    rng.shuffle(books)
    rng.shuffle(portal)
    return Dataset(
        period=period,
        books=books,
        portal=portal,
        vendors=vendors,
        bank=bank,
        irn_registry=irn_registry,
        truth=truth,
        near_miss_keys=near_miss_keys,
    )


@dataclass(slots=True)
class _InjectionContext:
    rng: random.Random
    key: str
    vendor: Vendor
    number: str
    doc_date: date
    tax: TaxAmounts
    irn: str
    period: str
    base_date: date
    books: list[PurchaseLine]
    portal: list[Gstr2bLine]
    bank: list[BankPayment]
    irn_registry: dict[str, IrnStatus]
    truth: list[TruthLabel]

    def label(self, cls: ExceptionClass, resolvable: bool, note: str, probe: str | None) -> None:
        self.truth.append(TruthLabel(self.key, cls, resolvable, note, probe))


def _inject_missing_in_2b(c: _InjectionContext) -> None:
    """In the books, absent from 2B: the supplier has not filed yet."""
    c.irn_registry[c.irn] = IrnStatus.ACTIVE
    c.books.append(PurchaseLine(c.key, c.vendor.gstin, c.number, c.doc_date, c.tax, irn=c.irn))
    c.bank.append(
        BankPayment(
            f"BP{c.key}",
            c.vendor.gstin,
            c.tax.invoice_value,
            c.doc_date + timedelta(days=12),
            f"NEFT {c.vendor.legal_name}",
        )
    )
    c.label(
        ExceptionClass.MISSING_IN_2B,
        True,
        "supplier had not filed GSTR-1 at 2B generation",
        "vendor_filing_history",
    )


def _inject_missing_in_books(c: _InjectionContext) -> None:
    """In 2B, absent from the books: a genuine book gap, evidenced by payment."""
    c.irn_registry[c.irn] = IrnStatus.ACTIVE
    c.portal.append(
        Gstr2bLine(
            c.key,
            c.vendor.gstin,
            c.number,
            c.doc_date,
            c.tax,
            irn=c.irn,
            irn_status=IrnStatus.ACTIVE,
            return_period=c.period,
        )
    )
    c.bank.append(
        BankPayment(
            f"BP{c.key}",
            c.vendor.gstin,
            c.tax.invoice_value,
            c.doc_date + timedelta(days=9),
            f"NEFT {c.vendor.legal_name}",
        )
    )
    c.label(
        ExceptionClass.MISSING_IN_BOOKS,
        True,
        "bank payment corroborates a missing book entry, not fraud",
        "query_bank_ledger",
    )


def _inject_amount_mismatch(c: _InjectionContext) -> None:
    """Same document, tax differs by more than the configured tolerance."""
    drift = money(Decimal(c.rng.randrange(250, 4_000)))
    other = TaxAmounts(
        taxable=c.tax.taxable, cgst=money(c.tax.cgst + drift), sgst=c.tax.sgst, igst=c.tax.igst
    )
    c.books.append(PurchaseLine(c.key, c.vendor.gstin, c.number, c.doc_date, c.tax, irn=c.irn))
    c.portal.append(
        Gstr2bLine(
            f"{c.key}P",
            c.vendor.gstin,
            c.number,
            c.doc_date,
            other,
            irn=c.irn,
            irn_status=IrnStatus.ACTIVE,
            return_period=c.period,
        )
    )
    c.label(
        ExceptionClass.AMOUNT_MISMATCH,
        True,
        f"supplier reported tax higher by {drift}",
        "compute_tolerance_match",
    )


def _inject_gstin_mismatch(c: _InjectionContext) -> None:
    """Two adjacent PAN characters transposed in the book entry.

    The swap position is chosen only from indices whose two characters differ.
    Transposing a repeated character ("...LLL...") is a no-op, which would
    produce a case labelled GSTIN_MISMATCH whose two sides are in fact
    identical -- Tier 1 matches it cleanly and the eval scores a false negative
    against ground truth that was wrong to begin with.
    """
    chars = list(c.vendor.gstin)
    swappable = [i for i in range(2, 12) if chars[i] != chars[i + 1]]
    if not swappable:
        raise ValueError(f"no distinct adjacent pair to transpose in {c.vendor.gstin}")
    i = c.rng.choice(swappable)
    chars[i], chars[i + 1] = chars[i + 1], chars[i]
    typo = "".join(chars)
    if typo == c.vendor.gstin:  # defensive: the guarantee above must hold
        raise AssertionError("transposition did not change the GSTIN")
    c.books.append(PurchaseLine(c.key, typo, c.number, c.doc_date, c.tax, irn=c.irn))
    c.portal.append(
        Gstr2bLine(
            f"{c.key}P",
            c.vendor.gstin,
            c.number,
            c.doc_date,
            c.tax,
            irn=c.irn,
            irn_status=IrnStatus.ACTIVE,
            return_period=c.period,
        )
    )
    c.label(
        ExceptionClass.GSTIN_MISMATCH,
        True,
        "transposed characters in the book GSTIN",
        "query_vendor_master",
    )


def _inject_duplicate(c: _InjectionContext) -> None:
    """Same invoice reported twice, as happens when two IRPs both forward it."""
    c.irn_registry[c.irn] = IrnStatus.ACTIVE
    base = Gstr2bLine(
        c.key,
        c.vendor.gstin,
        c.number,
        c.doc_date,
        c.tax,
        irn=c.irn,
        irn_status=IrnStatus.ACTIVE,
        return_period=c.period,
    )
    c.portal.append(base)
    c.portal.append(
        Gstr2bLine(
            f"{c.key}D",
            c.vendor.gstin,
            c.number.replace("/", "-"),
            c.doc_date,
            c.tax,
            irn=c.irn,
            irn_status=IrnStatus.ACTIVE,
            return_period=c.period,
        )
    )
    c.books.append(
        PurchaseLine(f"{c.key}B", c.vendor.gstin, c.number, c.doc_date, c.tax, irn=c.irn)
    )
    c.label(
        ExceptionClass.DUPLICATE,
        True,
        "identical IRN reported under two number formats",
        "check_irn_status",
    )


def _inject_cancelled_irn(c: _InjectionContext) -> None:
    c.irn_registry[c.irn] = IrnStatus.CANCELLED
    c.portal.append(
        Gstr2bLine(
            c.key,
            c.vendor.gstin,
            c.number,
            c.doc_date,
            c.tax,
            irn=c.irn,
            irn_status=IrnStatus.CANCELLED,
            return_period=c.period,
        )
    )
    c.label(
        ExceptionClass.CANCELLED_IRN,
        True,
        "IRN cancelled at the portal but still present in 2B",
        "check_irn_status",
    )


def _inject_credit_note_unlinked(c: _InjectionContext) -> None:
    """A credit note whose original sits in an earlier period."""
    c.portal.append(
        Gstr2bLine(
            c.key,
            c.vendor.gstin,
            f"CN/{c.number}",
            c.doc_date,
            c.tax,
            kind=DocumentKind.CREDIT_NOTE,
            return_period=c.period,
            original_invoice_number=c.number,
        )
    )
    c.label(
        ExceptionClass.CREDIT_NOTE_UNLINKED,
        True,
        "original invoice lies in a prior return period",
        "query_prior_2b",
    )


def _inject_rcm(c: _InjectionContext) -> None:
    c.books.append(
        PurchaseLine(c.key, c.vendor.gstin, c.number, c.doc_date, c.tax, reverse_charge=True)
    )
    c.portal.append(
        Gstr2bLine(
            f"{c.key}P",
            c.vendor.gstin,
            c.number,
            c.doc_date,
            c.tax,
            reverse_charge=True,
            return_period=c.period,
        )
    )
    c.label(ExceptionClass.RCM, True, "reverse charge; credit follows separate treatment", None)


def _inject_time_barred(c: _InjectionContext) -> None:
    """Invoice from a financial year whose Section 16(4) window has closed."""
    old_date = date(c.base_date.year - 2, 5, 12)
    c.books.append(PurchaseLine(c.key, c.vendor.gstin, c.number, old_date, c.tax))
    c.label(
        ExceptionClass.TIME_BARRED,
        False,
        "outside the Section 16(4) claim window; report only",
        None,
    )


_INJECTORS = {
    ExceptionClass.MISSING_IN_2B: _inject_missing_in_2b,
    ExceptionClass.MISSING_IN_BOOKS: _inject_missing_in_books,
    ExceptionClass.AMOUNT_MISMATCH: _inject_amount_mismatch,
    ExceptionClass.GSTIN_MISMATCH: _inject_gstin_mismatch,
    ExceptionClass.DUPLICATE: _inject_duplicate,
    ExceptionClass.CANCELLED_IRN: _inject_cancelled_irn,
    ExceptionClass.CREDIT_NOTE_UNLINKED: _inject_credit_note_unlinked,
    ExceptionClass.RCM: _inject_rcm,
    ExceptionClass.TIME_BARRED: _inject_time_barred,
}

# A realistic month: most documents match, and late supplier filing dominates
# the exceptions because roughly a third of suppliers file GSTR-1 late.
DISTRIBUTION_MIX: dict[ExceptionClass, int] = {
    ExceptionClass.MISSING_IN_2B: 74,
    ExceptionClass.MISSING_IN_BOOKS: 34,
    ExceptionClass.AMOUNT_MISMATCH: 26,
    ExceptionClass.GSTIN_MISMATCH: 16,
    ExceptionClass.DUPLICATE: 18,
    ExceptionClass.CANCELLED_IRN: 12,
    ExceptionClass.CREDIT_NOTE_UNLINKED: 10,
    ExceptionClass.RCM: 6,
    ExceptionClass.TIME_BARRED: 4,
}

# The residue Tier 1 cannot close, oversampled so the agent experiments have
# enough cases per class to say anything. Population estimates are recovered by
# reweighting back to DISTRIBUTION_MIX.
HARD_MIX: dict[ExceptionClass, int] = {
    ExceptionClass.MISSING_IN_BOOKS: 18,
    ExceptionClass.AMOUNT_MISMATCH: 16,
    ExceptionClass.GSTIN_MISMATCH: 14,
    ExceptionClass.CREDIT_NOTE_UNLINKED: 12,
}
