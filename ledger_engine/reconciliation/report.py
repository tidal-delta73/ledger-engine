"""Deterministic assembly of the serialized reconciliation report.

Only shape assembly lives here: fixed key insertion order, two-sided
record evidence, stable business-key ordering and per-currency integer
summation.  All matching decisions come from
:mod:`ledger_engine.reconciliation.matching`; this module never pairs
records and never invents an exchange rate.  Every amount is rendered
from exact integer minor units (or exact rate fractions), so repeated
calls on equal facts serialize byte-for-byte regardless of locale, hash
seed or wall-clock time.
"""
from __future__ import annotations

from fractions import Fraction

from .matching import (
    AmbiguityGroup,
    ConfirmedPair,
    MatchResult,
    external_effective_base,
)
from .model import ExternalLine, LedgerFact
from .money import (
    effective_rate,
    format_base_cents,
    format_minor,
    format_rate,
)

__all__ = [
    "EXACT_MATCH",
    "MISSING_ON_BOOKS",
    "MISSING_EXTERNALLY",
    "ORIGINAL_AMOUNT_MISMATCH",
    "CURRENCY_MISMATCH",
    "BASE_CONVERSION_MISMATCH",
    "AMBIGUOUS",
    "STATUS_ORDER",
    "classify_pair",
    "build_report",
]

EXACT_MATCH = "exact_match"
MISSING_ON_BOOKS = "missing_on_books"
MISSING_EXTERNALLY = "missing_externally"
ORIGINAL_AMOUNT_MISMATCH = "original_amount_mismatch"
CURRENCY_MISMATCH = "currency_mismatch"
BASE_CONVERSION_MISMATCH = "base_conversion_mismatch"
AMBIGUOUS = "ambiguous"

# Difference categories in their canonical report order; exact matches
# live in their own section.
STATUS_ORDER = (
    MISSING_ON_BOOKS,
    MISSING_EXTERNALLY,
    ORIGINAL_AMOUNT_MISMATCH,
    CURRENCY_MISMATCH,
    BASE_CONVERSION_MISMATCH,
    AMBIGUOUS,
)
_STATUS_RANK = {status: rank for rank, status in enumerate(STATUS_ORDER)}

_RATE_RECORD_FIELDS = ("recorded_rate", "effective_rate")
_EXTERNAL_RECORD_FIELDS = (
    "source_id",
    "business_date",
    "voucher_ref",
    "currency",
    "original_amount",
    "base_amount",
    "rate",
)
_BOOK_RECORD_FIELDS = (
    "entry_id",
    "voucher_id",
    "line_no",
    "business_date",
    "account_code",
    "currency",
    "original_amount",
    "base_amount",
    "rate",
)
_ITEM_FIELDS = ("status", "external", "book", "comparison", "ambiguous_with")
_MATCH_FIELDS = ("status", "external", "book", "comparison")
_SUMMARY_FIELDS = (
    "currency",
    "matched_count",
    "matched_original_total",
    "matched_base_total",
    "missing_on_books_count",
    "missing_on_books_external_original",
    "missing_on_books_external_base",
    "missing_externally_count",
    "missing_externally_book_original",
    "missing_externally_book_base",
    "amount_mismatch_count",
    "external_original_total",
    "book_original_total",
    "original_gap",
    "external_base_total",
    "book_base_total",
    "base_gap",
    "currency_mismatch_count",
    "ambiguous_count",
)
_BASE_TOTAL_FIELDS = (
    "currency",
    "external_base_total",
    "book_base_total",
    "base_gap",
)
_REPORT_FIELDS = (
    "book_set_code",
    "book_set_name",
    "account_code",
    "account_name",
    "base_currency",
    "cutoff",
    "counts",
    "matches",
    "differences",
    "summaries",
    "base_totals",
)
_COUNT_FIELDS = (
    EXACT_MATCH,
    MISSING_ON_BOOKS,
    MISSING_EXTERNALLY,
    ORIGINAL_AMOUNT_MISMATCH,
    CURRENCY_MISMATCH,
    BASE_CONVERSION_MISMATCH,
    AMBIGUOUS,
)


def classify_pair(pair: ConfirmedPair) -> str:
    """Assign a pair its status by the fixed comparison precedence.

    Currency first, then the original-currency amount, then the books'
    already-saved base amount against the external base claim.  A
    missing external base claim cannot cause a conversion mismatch.
    """
    fact, line = pair.fact, pair.line
    if fact.currency != line.currency:
        return CURRENCY_MISMATCH
    if fact.original_minor != line.original_minor:
        return ORIGINAL_AMOUNT_MISMATCH
    external_base = external_effective_base(line)
    if external_base is not None and external_base != fact.base_minor:
        return BASE_CONVERSION_MISMATCH
    return EXACT_MATCH


def _rate_record(record_rate: Fraction | None,
                 effective_rate_value: Fraction | None) -> dict:
    """Rate evidence: only rates already recorded with the records."""
    values = {
        "recorded_rate": format_rate(record_rate)
        if record_rate is not None else None,
        "effective_rate": format_rate(effective_rate_value)
        if effective_rate_value is not None else None,
    }
    return {field: values[field] for field in _RATE_RECORD_FIELDS}


def _external_record(line: ExternalLine) -> dict:
    effective_base = external_effective_base(line)
    values = {
        "source_id": line.source_id,
        "business_date": line.business_date,
        "voucher_ref": line.voucher_ref,
        "currency": line.currency,
        "original_amount": format_minor(line.original_minor, line.currency),
        "base_amount": format_base_cents(effective_base)
        if effective_base is not None else None,
        "rate": _rate_record(line.recorded_rate, None),
    }
    return {field: values[field] for field in _EXTERNAL_RECORD_FIELDS}


def _book_effective_rate(fact: LedgerFact) -> Fraction | None:
    return effective_rate(
        fact.original_minor, fact.base_minor, fact.currency
    )


def _book_record(fact: LedgerFact) -> dict:
    effective = _book_effective_rate(fact)
    values = {
        "entry_id": fact.entry_id,
        "voucher_id": fact.voucher_id,
        "line_no": fact.line_no,
        "business_date": fact.date,
        "account_code": fact.account_code,
        "currency": fact.currency,
        "original_amount": format_minor(fact.original_minor, fact.currency),
        "base_amount": format_base_cents(fact.base_minor),
        "rate": _rate_record(fact.recorded_rate, effective),
    }
    return {field: values[field] for field in _BOOK_RECORD_FIELDS}


def _comparison(pair: ConfirmedPair) -> dict:
    """The amounts and rates actually compared, rendered canonically."""
    fact, line = pair.fact, pair.line
    external_base = external_effective_base(line)
    values = {
        "currency_external": line.currency,
        "currency_book": fact.currency,
        "external_original_amount": format_minor(line.original_minor,
                                                 line.currency),
        "book_original_amount": format_minor(fact.original_minor,
                                             fact.currency),
        "external_base_amount": format_base_cents(external_base)
        if external_base is not None else None,
        "book_base_amount": format_base_cents(fact.base_minor),
        "external_rate": format_rate(line.recorded_rate)
        if line.recorded_rate is not None else None,
        "book_recorded_rate": format_rate(fact.recorded_rate)
        if fact.recorded_rate is not None else None,
        "book_effective_rate": format_rate(
            _book_effective_rate(fact))
        if fact.original_minor != 0 else None,
    }
    return values


def _pair_item(pair: ConfirmedPair, status: str,
               *, in_differences: bool) -> dict:
    values = {
        "status": status,
        "external": _external_record(pair.line),
        "book": _book_record(pair.fact),
        "comparison": _comparison(pair),
        "ambiguous_with": None,
    }
    fields = _ITEM_FIELDS if in_differences else _MATCH_FIELDS
    return {field: values[field] for field in fields}


def _missing_external_item(line: ExternalLine) -> dict:
    values = {
        "status": MISSING_ON_BOOKS,
        "external": _external_record(line),
        "book": None,
        "comparison": None,
        "ambiguous_with": None,
    }
    return {field: values[field] for field in _ITEM_FIELDS}


def _missing_book_item(fact: LedgerFact) -> dict:
    values = {
        "status": MISSING_EXTERNALLY,
        "external": None,
        "book": _book_record(fact),
        "comparison": None,
        "ambiguous_with": None,
    }
    return {field: values[field] for field in _ITEM_FIELDS}


def _ambiguity_item(group: AmbiguityGroup) -> dict:
    members: list[dict] = []
    for fact in sorted(group.facts, key=lambda item: item.business_key()):
        members.append({"side": "book", "record": _book_record(fact)})
    for line in sorted(group.lines, key=lambda item: item.business_key()):
        members.append({"side": "external",
                        "record": _external_record(line)})
    members.sort(key=lambda member: (
        member["side"],
        member["record"]["business_date"],
        member["record"].get("entry_id")
        if member["side"] == "book"
        else member["record"]["source_id"],
    ))
    values = {
        "status": AMBIGUOUS,
        "external": None,
        "book": None,
        "comparison": None,
        "ambiguous_with": {"reason": group.reason, "records": members},
    }
    return {field: values[field] for field in _ITEM_FIELDS}


def _ambiguity_sort_key(group: AmbiguityGroup) -> tuple[str, str, str, str]:
    fact_keys = [fact.business_key() for fact in group.facts]
    line_keys = [line.business_key() for line in group.lines]
    all_keys = fact_keys + line_keys
    first_date, first_id = min(all_keys)
    sides = ("F" if fact_keys else "") + ("L" if line_keys else "")
    return (first_date, first_id, sides, group.reason)


class _CurrencyTotals:
    """Exact integer accumulators for one currency bucket."""

    __slots__ = (
        "matched_count", "matched_original", "matched_base",
        "missing_on_books_count", "missing_on_books_external_original",
        "missing_on_books_external_base",
        "missing_externally_count", "missing_externally_book_original",
        "missing_externally_book_base",
        "amount_mismatch_count",
        "external_original", "book_original",
        "external_base", "book_base",
        "currency_mismatch_count", "ambiguous_count",
    )

    def __init__(self) -> None:
        self.matched_count = 0
        self.matched_original = 0
        self.matched_base = 0
        self.missing_on_books_count = 0
        self.missing_on_books_external_original = 0
        self.missing_on_books_external_base = 0
        self.missing_externally_count = 0
        self.missing_externally_book_original = 0
        self.missing_externally_book_base = 0
        self.amount_mismatch_count = 0
        self.external_original = 0
        self.book_original = 0
        self.external_base = 0
        self.book_base = 0
        self.currency_mismatch_count = 0
        self.ambiguous_count = 0


def _summaries(
    pair_status: list[tuple[ConfirmedPair, str]],
    missing_on_books: list[ExternalLine],
    missing_externally: list[LedgerFact],
    ambiguities: list[AmbiguityGroup],
    base_currency: str,
) -> tuple[list[dict], dict]:
    """Build per-currency totals; original amounts never cross currencies.

    Returns the sorted per-currency rows plus the grand totals in the
    single base currency (the only cross-currency sum permitted).
    """
    totals: dict[str, _CurrencyTotals] = {}

    def bucket(currency: str) -> _CurrencyTotals:
        return totals.setdefault(currency, _CurrencyTotals())

    for pair, status in pair_status:
        fact, line = pair.fact, pair.line
        external_base = external_effective_base(line)
        if status == EXACT_MATCH:
            row = bucket(fact.currency)
            row.matched_count += 1
            row.matched_original += fact.original_minor
            row.matched_base += fact.base_minor
            row.external_original += line.original_minor
            row.book_original += fact.original_minor
            # A confirmed business with no external base claim is still
            # reconciled: the books' recorded base amount is the agreed
            # base, so it must not manufacture a base gap.  Only an
            # explicit external claim can disagree (that pair would have
            # been classified BASE_CONVERSION_MISMATCH instead).
            row.external_base += (
                external_base if external_base is not None
                else fact.base_minor
            )
            row.book_base += fact.base_minor
        elif status == ORIGINAL_AMOUNT_MISMATCH:
            # Same currency by definition of the precedence.
            row = bucket(fact.currency)
            row.amount_mismatch_count += 1
            row.external_original += line.original_minor
            row.book_original += fact.original_minor
            if external_base is not None:
                row.external_base += external_base
            row.book_base += fact.base_minor
        elif status == BASE_CONVERSION_MISMATCH:
            row = bucket(fact.currency)
            row.amount_mismatch_count += 1
            row.external_original += line.original_minor
            row.book_original += fact.original_minor
            row.external_base += external_base
            row.book_base += fact.base_minor
        else:  # CURRENCY_MISMATCH: the two sides live in different buckets.
            external_row = bucket(line.currency)
            book_row = bucket(fact.currency)
            external_row.currency_mismatch_count += 1
            book_row.currency_mismatch_count += 1
            external_row.external_original += line.original_minor
            if external_base is not None:
                external_row.external_base += external_base
            book_row.book_original += fact.original_minor
            book_row.book_base += fact.base_minor

    for line in missing_on_books:
        row = bucket(line.currency)
        row.missing_on_books_count += 1
        row.missing_on_books_external_original += line.original_minor
        row.external_original += line.original_minor
        external_base = external_effective_base(line)
        if external_base is not None:
            row.missing_on_books_external_base += external_base
            row.external_base += external_base

    for fact in missing_externally:
        row = bucket(fact.currency)
        row.missing_externally_count += 1
        row.missing_externally_book_original += fact.original_minor
        row.book_original += fact.original_minor
        row.missing_externally_book_base += fact.base_minor
        row.book_base += fact.base_minor

    for group in ambiguities:
        # Every ambiguous record is still an unmatched fact of the
        # reconciliation as of the cutoff, so its amounts feed the
        # overall external/book and base totals even though no pair (and
        # hence no per-pair gap) can be formed.
        touched: set[str] = set()
        for fact in group.facts:
            bucket(fact.currency).book_original += fact.original_minor
            bucket(fact.currency).book_base += fact.base_minor
            touched.add(fact.currency)
        for line in group.lines:
            row = bucket(line.currency)
            row.external_original += line.original_minor
            external_base = external_effective_base(line)
            if external_base is not None:
                row.external_base += external_base
            touched.add(line.currency)
        for currency in touched:
            bucket(currency).ambiguous_count += 1

    rows: list[dict] = []
    for currency in sorted(totals):
        row = totals[currency]
        values = {
            "currency": currency,
            "matched_count": row.matched_count,
            "matched_original_total": format_minor(row.matched_original,
                                                   currency),
            "matched_base_total": format_base_cents(row.matched_base),
            "missing_on_books_count": row.missing_on_books_count,
            "missing_on_books_external_original": format_minor(
                row.missing_on_books_external_original, currency),
            "missing_on_books_external_base": format_base_cents(
                row.missing_on_books_external_base),
            "missing_externally_count": row.missing_externally_count,
            "missing_externally_book_original": format_minor(
                row.missing_externally_book_original, currency),
            "missing_externally_book_base": format_base_cents(
                row.missing_externally_book_base),
            "amount_mismatch_count": row.amount_mismatch_count,
            "external_original_total": format_minor(row.external_original,
                                                    currency),
            "book_original_total": format_minor(row.book_original,
                                                currency),
            "original_gap": format_minor(
                row.external_original - row.book_original, currency),
            "external_base_total": format_base_cents(row.external_base),
            "book_base_total": format_base_cents(row.book_base),
            "base_gap": format_base_cents(row.external_base - row.book_base),
            "currency_mismatch_count": row.currency_mismatch_count,
            "ambiguous_count": row.ambiguous_count,
        }
        rows.append({field: values[field] for field in _SUMMARY_FIELDS})

    grand_external_base = sum(row.external_base for row in totals.values())
    grand_book_base = sum(row.book_base for row in totals.values())
    base_totals_values = {
        "currency": base_currency,
        "external_base_total": format_base_cents(grand_external_base),
        "book_base_total": format_base_cents(grand_book_base),
        "base_gap": format_base_cents(grand_external_base - grand_book_base),
    }
    base_totals = {
        field: base_totals_values[field] for field in _BASE_TOTAL_FIELDS
    }
    return rows, base_totals


def build_report(
    book_set,
    account,
    cutoff: str,
    match_result: MatchResult,
    base_currency: str,
) -> dict:
    """Assemble the final JSON-serializable report document."""
    pair_status: list[tuple[ConfirmedPair, str]] = [
        (pair, classify_pair(pair)) for pair in match_result.pairs
    ]

    matches = [
        _pair_item(pair, status, in_differences=False)
        for pair, status in sorted(
            pair_status,
            key=lambda item: (item[0].line.business_date,
                              item[0].line.source_id),
        )
        if status == EXACT_MATCH
    ]

    difference_items: list[dict] = []
    for pair, status in pair_status:
        if status != EXACT_MATCH:
            difference_items.append(
                _pair_item(pair, status, in_differences=True))
    for line in match_result.missing_on_books:
        difference_items.append(_missing_external_item(line))
    for fact in match_result.missing_externally:
        difference_items.append(_missing_book_item(fact))
    for group in match_result.ambiguities:
        difference_items.append(_ambiguity_item(group))

    def difference_sort_key(item: dict):
        status = item["status"]
        rank = _STATUS_RANK[status]
        if item["external"] is not None:
            record = item["external"]
            return (rank, record["business_date"], record["source_id"], "")
        if item["book"] is not None:
            record = item["book"]
            return (rank, record["business_date"], "", record["entry_id"])
        # Ambiguity: order by the full sorted set of member business
        # keys, so groups with the same earliest date still have a
        # content-derived, input-order-independent tie break.
        group = item["ambiguous_with"]
        member_keys = sorted(
            (member["record"]["business_date"], member["side"],
             member["record"].get("entry_id")
             or member["record"]["source_id"])
            for member in group["records"]
        )
        signature = "\u0000".join(f"{d}|{s}|{i}" for d, s, i in member_keys)
        return (rank, signature, "", "")

    difference_items.sort(key=difference_sort_key)

    counts = {status: 0 for status in _COUNT_FIELDS}
    for _pair, status in pair_status:
        counts[status] += 1
    counts[MISSING_ON_BOOKS] = len(match_result.missing_on_books)
    counts[MISSING_EXTERNALLY] = len(match_result.missing_externally)
    counts[AMBIGUOUS] = len(match_result.ambiguities)
    counts_doc = {status: counts[status] for status in _COUNT_FIELDS}

    summaries, base_totals = _summaries(
        pair_status,
        list(match_result.missing_on_books),
        list(match_result.missing_externally),
        list(match_result.ambiguities),
        base_currency,
    )

    values = {
        "book_set_code": book_set.code,
        "book_set_name": book_set.name,
        "account_code": account.code,
        "account_name": account.name,
        "base_currency": base_currency,
        "cutoff": cutoff,
        "counts": counts_doc,
        "matches": matches,
        "differences": difference_items,
        "summaries": summaries,
        "base_totals": base_totals,
    }
    return {field: values[field] for field in _REPORT_FIELDS}
