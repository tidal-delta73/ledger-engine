"""Book-to-external difference localization (reconciliation).

Public entry point: :func:`build_reconciliation_report`.

The feature is strictly read-only with respect to every existing
behavior: posting, reversal, closing, balance queries and the existing
reports are untouched -- this subpackage never imports them for
execution, it only shares the immutable chart parser and the calendar
date validator.  A report is generated for one book set, one account and
one cutoff; it reads only posted ledger facts that were effective on or
before the cutoff and not cancelled by a reversal dated on or before the
cutoff, and compares them against the supplied external snapshot.

Fixed observable boundaries:

* malformed payloads or an external snapshot with duplicate source ids
  raise :class:`InvalidReconciliationInputError` and never produce a
  partial report;
* an unknown book set or account raises :class:`TargetNotFoundError`;
* a cutoff inside an unclosed historical period raises
  :class:`PeriodStatusConflictError`.

Results are pure functions of the supplied payloads: no current exchange
rate is ever consulted, and serialization is byte-stable across input
permutations, hash seeds, locales and wall-clock time.
"""
from __future__ import annotations

from .errors import (
    InvalidReconciliationInputError,
    PeriodStatusConflictError,
    TargetNotFoundError,
)
from .matching import match_records
from .model import (
    parse_catalog,
    parse_external_lines,
    validate_cutoff,
)
from .report import build_report

__all__ = [
    "build_reconciliation_report",
    "InvalidReconciliationInputError",
    "TargetNotFoundError",
    "PeriodStatusConflictError",
]


def _containing_period(periods, cutoff: str):
    """Latest period whose start is on or before the cutoff, if any."""
    containing = None
    for period in periods:  # periods are already sorted by start
        if period.start <= cutoff:
            containing = period
        else:
            break
    return containing


def _check_period_status(book_set, cutoff: str) -> None:
    """Reject cutoffs landing in an unclosed historical period.

    "Historical" is defined purely by the catalog's own period list, so
    the judgement never reads the wall clock: a period is historical once
    a later period exists.  The open/current (latest) period may be
    unclosed; an earlier one may not.  A cutoff earlier than every known
    period has no covering period at all and is likewise a conflict.
    """
    periods = book_set.periods
    if not periods:
        # Without period metadata no historical-period constraint can
        # apply; the caller is responsible for representing periods.
        return
    containing = _containing_period(periods, cutoff)
    if containing is None:
        raise PeriodStatusConflictError(
            f"cutoff {cutoff!r} is earlier than the first accounting "
            f"period {periods[0].start!r}"
        )
    is_latest = containing.start == periods[-1].start
    if not containing.closed and not is_latest:
        raise PeriodStatusConflictError(
            f"cutoff {cutoff!r} falls in unclosed historical period "
            f"{containing.start!r}"
        )


def build_reconciliation_report(
    book_sets: object,
    book_set_code: object,
    account_code: object,
    cutoff: object,
    external_details: object,
) -> dict:
    """Build a reproducible book-to-external reconciliation report.

    Parameters
    ----------
    book_sets:
        Raw book-set catalog list.  Each book set carries ``code``,
        ``name``, ``base_currency``, ``accounts`` (the same chart shape
        as the voucher engine), optional ``periods`` (``{start,
        closed}``), posted ``facts`` and optional ``reversals``.
    book_set_code, account_code:
        Exact codes locating the reconciliation target.
    cutoff:
        ``YYYY-MM-DD`` inclusive cutoff date.
    external_details:
        Snapshot list; each line requires ``source_id`` (unique within
        the snapshot), ``business_date``, ``currency`` and ``amount`` and
        may carry ``voucher_ref``, ``base_amount`` and ``recorded_rate``.
        An empty list is valid: every in-scope fact becomes
        "missing externally".

    Returns a fresh JSON-serializable report dict; inputs are never
    mutated and equal payloads yield byte-identical reports.
    """
    # Stage 1: catalog -> immutable snapshot.  Malformed catalog payloads
    # are fixed invalid-input errors.
    catalog = parse_catalog(book_sets)

    # Stage 2: the request-level target boundaries, in fixed order.
    if not isinstance(book_set_code, str) or book_set_code == "":
        raise InvalidReconciliationInputError(
            f"book_set_code must be a non-empty string, "
            f"got {book_set_code!r}"
        )
    book_set = catalog.get(book_set_code)
    if book_set is None:
        raise TargetNotFoundError(
            f"unknown book set code {book_set_code!r}"
        )

    cutoff_day = validate_cutoff(cutoff)

    if not isinstance(account_code, str) or account_code == "":
        raise InvalidReconciliationInputError(
            f"account_code must be a non-empty string, "
            f"got {account_code!r}"
        )
    account = book_set.get_account(account_code)
    if account is None:
        raise TargetNotFoundError(
            f"book set {book_set_code!r}: unknown account code "
            f"{account_code!r}"
        )

    # Stage 3: period status is a property of the cutoff and the books.
    _check_period_status(book_set, cutoff_day)

    # Stage 4: the external snapshot, including the fixed duplicate-id
    # boundary.
    lines = parse_external_lines(external_details)

    # Stage 5: scope.  Only ledger facts are time-scoped: the same
    # account, effective on/before the cutoff, and not cancelled by a
    # reversal that was itself effective by the cutoff.  The external
    # snapshot is asserted by the caller to be the as-of-cutoff snapshot,
    # so every supplied line participates; its business_date is merely
    # an identity attribute (and can surface a book side that is not yet
    # effective at the cutoff).
    scoped_facts = [
        fact for fact in book_set.facts
        if fact.account_code == account_code
        and fact.date <= cutoff_day
        and not (
            fact.entry_id in book_set.reversals
            and book_set.reversals[fact.entry_id].date <= cutoff_day
        )
    ]
    scoped_facts.sort(key=lambda fact: fact.business_key())
    scoped_lines = sorted(lines, key=lambda line: line.business_key())

    # Stage 6: deterministic identity matching and difference assembly.
    match_result = match_records(scoped_facts, scoped_lines)
    return build_report(
        book_set, account, cutoff_day, match_result,
        book_set.base_currency,
    )
