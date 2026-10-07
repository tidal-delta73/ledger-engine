"""Batch reversal: each validated voucher -> one reversing voucher.

The public :func:`reverse_vouchers` consumes the same arguments as
:func:`ledger_engine.vouchers.pipeline.normalize_vouchers` -- base
currency, chart of accounts and the raw source batch -- plus one uniform
reversal date.  Source validation is the *same* pipeline
(:func:`ledger_engine.vouchers.pipeline._validate_batch`), so the
observable check order, leaf exception categories, exact-match account
resolution, immutable chart snapshot and integer-cents totals are shared,
not copied.  Result documents are assembled through
:mod:`ledger_engine.vouchers.assemble`, the single deterministic builder
normalization uses, so a reversal carries the identical voucher/entry
fields in the identical key insertion order and re-normalizes into an
equal container; posted together with its sources, every per-account
turnover cancels to a zero net.

The observable sequence is contractual:

1. the complete source-batch validation sequence (base currency and
   chart, batch shape, then per voucher: all structural checks, batch
   duplicate id, currency, per-entry business rules, balance) -- the
   first leaf failure wins and no partial reversal is produced;
2. the reversal date must be a real ``YYYY-MM-DD`` calendar date;
3. the reversal date must not be earlier than any source voucher date,
   checked in source batch order.

Steps 2 and 3 both raise
:class:`~ledger_engine.vouchers.errors.VoucherFormatError`.  The
``REV:`` id prefix is a literal concatenation, so a source id that
already starts with ``REV:`` is never rewritten or folded.  Inputs
(chart, vouchers and their nested containers) are never mutated;
repeated calls return equal, brand-new containers.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from .assemble import build_voucher
from .entries import ValidatedVoucherBody
from .errors import VoucherFormatError
from .pipeline import _validate_batch
from .structure import StructuredVoucher, _valid_calendar_date

__all__ = ["reverse_vouchers"]

_REVERSAL_ID_PREFIX = "REV:"


def _reverse_body(body: ValidatedVoucherBody) -> ValidatedVoucherBody:
    """Swap the integer-cents debit/credit side of every validated line.

    Line numbers, account snapshot fields and summaries keep their source
    values and order; the totals swap sides as well (sources are balanced,
    so they stay equal).  Everything stays in integer cents.
    """
    reversed_entries = tuple(
        replace(
            entry,
            debit_cents=entry.credit_cents,
            credit_cents=entry.debit_cents,
        )
        for entry in body.entries
    )
    return ValidatedVoucherBody(
        entries=reversed_entries,
        debit_total=body.credit_total,
        credit_total=body.debit_total,
    )


def reverse_vouchers(
    base_currency: object,
    chart_of_accounts: object,
    vouchers: object,
    reversal_date: object,
) -> list[dict]:
    """Validate a raw batch and build one reversing voucher per source.

    Each source voucher yields exactly one reversal, in source order:
    the id becomes ``"REV:" + source id`` (literal concatenation), the
    date becomes ``reversal_date``, currency/accounts/summaries and the
    entry order are preserved, and every line's debit and credit amounts
    swap sides.  Amounts and totals render through the shared
    two-decimal fixed-point builder, so the result feeds
    :func:`normalize_vouchers`, :func:`post_vouchers` or
    :func:`build_trial_balance` unchanged.

    Source validation runs to completion first; only afterwards is the
    reversal date checked (valid calendar date, then not earlier than any
    source date).  Any failure raises the existing leaf exception --
    :class:`VoucherFormatError` for the reversal-date boundaries -- and
    nothing partial is returned.  An empty source batch returns ``[]``.
    """
    # Stage 1: the whole shared validation sequence; the immutable chart
    # snapshot, first-failure rule and leaf exceptions come for free.
    _, validated = _validate_batch(base_currency, chart_of_accounts, vouchers)

    # Stage 2: the reversal date itself must be a real calendar date.
    if not isinstance(reversal_date, str) or not _valid_calendar_date(
        reversal_date
    ):
        raise VoucherFormatError(
            "reversal date must be a valid YYYY-MM-DD calendar date, "
            f"got {reversal_date!r}"
        )
    reversal_day = date.fromisoformat(reversal_date)

    # Stage 3: it must not precede any source voucher, in batch order.
    for v_index, (structured, _body) in enumerate(validated):
        source_day = date.fromisoformat(structured.date)
        if reversal_day < source_day:
            raise VoucherFormatError(
                f"voucher #{v_index} ({structured.voucher_id}): reversal "
                f"date {reversal_date!r} is earlier than voucher date "
                f"{structured.date!r}"
            )

    # Stage 4: deterministic assembly through the shared builder.
    reversed_vouchers: list[dict] = []
    for structured, body in validated:
        reversed_structured = StructuredVoucher(
            voucher_id=_REVERSAL_ID_PREFIX + structured.voucher_id,
            date=reversal_date,
            currency=structured.currency,
            entries=structured.entries,
        )
        reversed_vouchers.append(
            build_voucher(reversed_structured, _reverse_body(body))
        )
    return reversed_vouchers
