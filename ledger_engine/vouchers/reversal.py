"""Batch reversal: one reversing voucher per validated source voucher.

The public :func:`reverse_vouchers` consumes the same validated batch as
:func:`ledger_engine.vouchers.pipeline.normalize_vouchers` -- the shared
:func:`ledger_engine.vouchers.pipeline._validate_batch` run guarantees
the identical check order and first leaf exception, and no partial
reversal is ever produced: the reversal date itself is checked only
after every source voucher has passed.  Each source voucher yields
exactly one reversing voucher, in batch order, with ``REV:`` prefixed to
the source id (never collapsed, even if the source id already carries
the prefix), the date replaced by the unified reversal date, and every
entry's debit/credit amounts swapped.  Results are assembled through
:func:`ledger_engine.vouchers.assemble.build_voucher`, so field names,
field order and two-decimal rendering match normalization exactly and
reversed batches re-normalize to equal containers.  Inputs are never
mutated and repeated calls return equal, brand-new containers.
"""
from __future__ import annotations

from dataclasses import replace

from .assemble import build_voucher
from .entries import ValidatedVoucherBody
from .errors import VoucherFormatError
from .pipeline import _validate_batch
from .structure import _valid_calendar_date

__all__ = ["reverse_vouchers"]

#: Prefix prepended to a source voucher_id to form its reversal's id.
REVERSAL_ID_PREFIX = "REV:"


def _validate_reversal_date(reversal_date: object, validated: list) -> str:
    """Check the unified reversal date against the validated batch.

    Runs only after the whole source batch is valid.  The date must be a
    valid YYYY-MM-DD calendar date and must not precede any source
    voucher's date; both failures are plain format errors.
    """
    if not isinstance(reversal_date, str) or not _valid_calendar_date(
        reversal_date
    ):
        raise VoucherFormatError(
            "reversal date must be a valid YYYY-MM-DD calendar date, "
            f"got {reversal_date!r}"
        )
    for v_index, (structured, _) in enumerate(validated):
        if reversal_date < structured.date:
            raise VoucherFormatError(
                f"reversal date {reversal_date!r} is earlier than voucher "
                f"#{v_index} ({structured.voucher_id}) date "
                f"{structured.date!r}"
            )
    return reversal_date


def _reverse_body(body: ValidatedVoucherBody) -> ValidatedVoucherBody:
    """Swap debit and credit on every validated entry and the totals."""
    entries = tuple(
        replace(
            entry,
            debit_cents=entry.credit_cents,
            credit_cents=entry.debit_cents,
        )
        for entry in body.entries
    )
    return ValidatedVoucherBody(
        entries=entries,
        debit_total=body.credit_total,
        credit_total=body.debit_total,
    )


def reverse_vouchers(
    base_currency: object,
    chart_of_accounts: object,
    vouchers: object,
    reversal_date: object,
) -> list[dict]:
    """Build one reversing voucher per source voucher in a raw batch.

    Accepts the same arguments as :func:`normalize_vouchers` plus a
    unified reversal date, and fails with the same first leaf exception
    on any source problem; nothing partial is returned.  The reversal
    date must be a valid YYYY-MM-DD calendar date not earlier than any
    source voucher date, else :class:`VoucherFormatError` is raised.
    On success returns a new list, in source batch order, of vouchers
    shaped exactly like :func:`normalize_vouchers` output, so they can
    be fed straight back into normalization, posting or the trial
    balance.  An empty batch returns an empty list.
    """
    _, validated = _validate_batch(
        base_currency, chart_of_accounts, vouchers
    )
    date_text = _validate_reversal_date(reversal_date, validated)

    return [
        build_voucher(
            replace(
                structured,
                voucher_id=REVERSAL_ID_PREFIX + structured.voucher_id,
                date=date_text,
            ),
            _reverse_body(body),
        )
        for structured, body in validated
    ]
