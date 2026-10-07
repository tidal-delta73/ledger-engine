"""Public entry point: build a reproducible book-vs-statement report.

:func:`build_reconciliation_report` is a *total* function: every outcome,
including a malformed request, a missing book/account and an unfinished
historical period, comes back as a JSON-serializable envelope distinguished
by its ``outcome`` code rather than as a raised exception or a partial
report.  On success the envelope holds the seven difference categories and
the per-currency summary.

The observable boundary order is contractual:

1. request shape (container, required fields, value types, cutoff date);
2. base currency and chart of accounts;
3. the period table;
4. the book payload (facts, offsets and reverser references);
5. the external snapshot, including duplicate source-id detection;
6. target existence (book set first, then the account);
7. the period-status rule for the cutoff;
8. cutoff scoping, matching, classification and deterministic assembly.

Nothing here changes posting, reversal, closing, balance queries or the
existing reports: only read-only facts supplied by the caller are used,
and historical amounts are converted only at rates already recorded on
those facts -- never at a rate fetched at call time.
"""
from __future__ import annotations

from datetime import date

from ..vouchers.chart import parse_chart
from ..vouchers.errors import ChartOfAccountsError, InvalidEntryAmountError
from ..vouchers.structure import _valid_calendar_date
from . import assembly
from .contract import (
    OUTCOME_INVALID_INPUT,
    OUTCOME_PERIOD_STATUS_CONFLICT,
    OUTCOME_RECONCILED,
    OUTCOME_TARGET_NOT_FOUND,
)
from .engine import effective_facts, pair_records
from .errors import ValidationAborted
from .models import parse_book_facts, parse_external_details
from .periods import covering_period, validate_periods

__all__ = ["build_reconciliation_report"]

_REQUIRED_REQUEST_FIELDS = (
    "base_currency",
    "chart_of_accounts",
    "book_set_id",
    "account_code",
    "cutoff",
    "book",
    "external_details",
)
_BOOK_FIELD = "book"

_INVALID_KEY_ORDER = ("outcome", "reason")
_TARGET_KEY_ORDER = (
    "outcome",
    "target",
    "book_set_id",
    "account_code",
)
_PERIOD_KEY_ORDER = (
    "outcome",
    "cutoff",
    "period_id",
    "start_date",
    "end_date",
    "closed",
)
_REPORT_KEY_ORDER = (
    "outcome",
    "book_set_id",
    "account_code",
    "base_currency",
    "cutoff",
    "cutoff_period_id",
    "differences",
    "summary",
)


def _invalid(reason: str) -> dict:
    values = {"outcome": OUTCOME_INVALID_INPUT, "reason": reason}
    return {key: values[key] for key in _INVALID_KEY_ORDER}


def _target_not_found(target: str, book_set_id: str,
                      account_code: str | None) -> dict:
    values = {
        "outcome": OUTCOME_TARGET_NOT_FOUND,
        "target": target,
        "book_set_id": book_set_id,
        "account_code": account_code,
    }
    return {key: values[key] for key in _TARGET_KEY_ORDER}


def _period_conflict(cutoff: str, period) -> dict:
    values = {
        "outcome": OUTCOME_PERIOD_STATUS_CONFLICT,
        "cutoff": cutoff,
        "period_id": period.period_id,
        "start_date": period.start_date,
        "end_date": period.end_date,
        "closed": period.closed,
    }
    return {key: values[key] for key in _PERIOD_KEY_ORDER}


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValidationAborted(
            f"{field} must be a non-empty string, got {value!r}")
    return value


def _validate_request_shape(request: object) -> dict:
    if not isinstance(request, dict):
        raise ValidationAborted("request must be an object")
    for field in _REQUIRED_REQUEST_FIELDS:
        if field not in request:
            raise ValidationAborted(f"request: missing field {field!r}")
    _require_text(request["book_set_id"], "book_set_id")
    _require_text(request["account_code"], "account_code")
    cutoff = request["cutoff"]
    if not isinstance(cutoff, str) or not _valid_calendar_date(cutoff):
        raise ValidationAborted(
            "cutoff must be a valid YYYY-MM-DD date, got "
            f"{cutoff!r}")
    return request


def _check_fact_references(facts) -> None:
    """Offsets and reversers must name facts present in the same book."""
    ids = {fact.fact_id for fact in facts}
    for fact in facts:
        for offset in fact.offsets:
            if offset.fact_id not in ids:
                raise ValidationAborted(
                    f"book fact {fact.fact_id!r}: offset references "
                    f"unknown fact_id {offset.fact_id!r}")
        for reverser_id in fact.reversed_by:
            if reverser_id not in ids:
                raise ValidationAborted(
                    f"book fact {fact.fact_id!r}: reversed_by references "
                    f"unknown fact_id {reverser_id!r}")


def _book_set_id(book: object) -> str | None:
    if isinstance(book, dict) and isinstance(book.get("book_set_id"), str):
        return book["book_set_id"]
    return None


def build_reconciliation_report(request: object) -> dict:
    """Reconcile posted ledger facts against an external snapshot.

    See the module docstring for the boundary order.  The returned dict is
    freshly built, JSON-serializable and byte-reproducible for equal
    semantics regardless of input ordering, hash seed, locale or time.
    """
    # Stage 1: request shape.
    try:
        req = _validate_request_shape(request)
        base_currency = req["base_currency"]
        chart_of_accounts = req["chart_of_accounts"]
        book_set_id = req["book_set_id"]
        account_code = req["account_code"]
        cutoff = req["cutoff"]
        book = req[_BOOK_FIELD]
        external_details = req["external_details"]

        # Stage 2: base currency + chart, reusing the shared chart parser.
        chart = parse_chart(base_currency, chart_of_accounts)

        # Stage 3: period table.
        periods = validate_periods(req.get("periods"))

        # Stage 4: book payload.
        if not isinstance(book, dict):
            raise ValidationAborted("book must be an object")
        if "book_set_id" not in book:
            raise ValidationAborted("book: missing field 'book_set_id'")
        _require_text(book["book_set_id"], "book.book_set_id")
        facts = parse_book_facts(book)
        _check_fact_references(facts)

        # Stage 5: external snapshot and source-id uniqueness.
        details = parse_external_details(external_details)
        seen_source_ids: set[str] = set()
        for detail in details:
            if detail.source_id in seen_source_ids:
                raise ValidationAborted(
                    "external_details: duplicate source_id "
                    f"{detail.source_id!r}")
            seen_source_ids.add(detail.source_id)
    except ValidationAborted as aborted:
        return _invalid(aborted.reason)
    except (ChartOfAccountsError, InvalidEntryAmountError) as exc:
        # The shared parsers describe a malformed base currency/chart or a
        # malformed amount with their own leaf messages; for this total
        # function every one of them is an invalid-input outcome.
        return _invalid(str(exc) or exc.__class__.__name__)

    # Stage 6: target existence -- book set first, then the account.
    if _book_set_id(book) != book_set_id:
        return _target_not_found("book_set", book_set_id, account_code)
    if chart.get(account_code) is None:
        return _target_not_found("account", book_set_id, account_code)

    cutoff_day = date.fromisoformat(cutoff)

    # Stage 7: a cutoff inside a recorded but unclosed historical period
    # is a period-status conflict; a gap (including the still-open current
    # period, which the table does not list yet) is allowed.
    period = covering_period(periods, cutoff_day)
    if period is not None and not period.closed:
        return _period_conflict(cutoff, period)

    # Stage 8: read-only scoping, matching and deterministic assembly.
    scoped_facts = effective_facts(facts, account_code, cutoff_day)
    pairs, ambiguous, ledger_missing, external_missing = pair_records(
        details, scoped_facts)
    sections = assembly.build_difference_sections(
        pairs, ambiguous, ledger_missing, external_missing,
        chart.base_currency)
    summary = assembly.build_summary(
        scoped_facts, details, sections, pairs, chart.base_currency)

    differences = {
        category: sections[category]
        for category in assembly.DIFF_CATEGORIES
    }
    values = {
        "outcome": OUTCOME_RECONCILED,
        "book_set_id": book_set_id,
        "account_code": account_code,
        "base_currency": chart.base_currency,
        "cutoff": cutoff,
        "cutoff_period_id": period.period_id if period is not None else None,
        "differences": differences,
        "summary": summary,
    }
    return {key: values[key] for key in _REPORT_KEY_ORDER}
