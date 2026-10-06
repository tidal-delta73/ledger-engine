"""Ledger aggregation: validated entries -> per-account integer turnovers.

This is the single internal source for accumulating debit/credit
activity per account.  Posting (:mod:`ledger_engine.vouchers.posting`)
and the trial balance (:mod:`ledger_engine.vouchers.trial_balance`)
both consume the mapping produced here, so future stages (reversals,
period handling, recomputation) extend one accumulation loop instead of
drifting into two copies.  Presentation rules -- the normal-side ending
balance, the debit/credit ending columns, the totals summary -- stay
with the callers; this module only knows how validated entries add up
per account code.

Every chart account is present with a zero start, so unused and
inactive accounts report ``0.00`` downstream.  All arithmetic is
integer cents, and the returned mapping is freshly built per call with
immutable tuple values: callers can never share mutable state with each
other or with a later call.
"""
from __future__ import annotations

from .chart import Chart
from .entries import ValidatedVoucherBody
from .structure import StructuredVoucher

__all__ = ["accumulate_turnovers"]


def accumulate_turnovers(
    chart: Chart,
    validated: list[tuple[StructuredVoucher, ValidatedVoucherBody]],
) -> dict[str, tuple[int, int]]:
    """Accumulate integer-cents ``(debit, credit)`` per account code.

    ``validated`` is the ``(structured, body)`` sequence produced by
    :func:`ledger_engine.vouchers.pipeline._validate_batch`; entries are
    added in voucher-then-line order, which is associative and therefore
    order-independent in the result.  The returned dict follows chart
    order and covers every account -- touched or not -- so callers never
    re-implement the zero initialization.
    """
    turnovers: dict[str, tuple[int, int]] = {
        code: (0, 0) for code in chart.accounts
    }
    for _structured, body in validated:
        for entry in body.entries:
            debit, credit = turnovers[entry.account_code]
            turnovers[entry.account_code] = (
                debit + entry.debit_cents,
                credit + entry.credit_cents,
            )
    return turnovers
