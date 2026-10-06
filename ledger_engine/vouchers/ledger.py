"""Ledger aggregation: validated vouchers -> per-account turnovers.

This is the single internal source of the ledger's accumulation fact.
Both :func:`ledger_engine.vouchers.posting.post_vouchers` and
:func:`ledger_engine.vouchers.trial_balance.build_trial_balance` derive
their per-account debit/credit turnovers from
:func:`accumulate_turnovers`, so later reversals, period handling or
recomputation can never let two accumulation loops drift apart.

Only accumulation lives here: every chart account is zero-initialized
in chart order and validated integer-cents entries are summed in
voucher-then-line order.  Presentation stays with each consumer -- the
posting module owns the ``normal_side`` ending balance, while the trial
balance module owns the debit-minus-credit ending columns and totals,
and neither rule can leak through this boundary.

The returned tuple and its rows are frozen and freshly built per call,
so consumers never share a mutable dict or list; raw inputs are never
touched.
"""
from __future__ import annotations

from dataclasses import dataclass

from .chart import Account, Chart
from .entries import ValidatedVoucherBody
from .structure import StructuredVoucher

__all__ = ["AccountTurnover", "accumulate_turnovers"]


@dataclass(frozen=True)
class AccountTurnover:
    """Integer-cents debit/credit turnover accumulated for one account."""

    account: Account
    debit_cents: int
    credit_cents: int


def accumulate_turnovers(
    chart: Chart,
    validated: list[tuple[StructuredVoucher, ValidatedVoucherBody]],
) -> tuple[AccountTurnover, ...]:
    """Sum validated entry amounts per account, one row per chart account.

    Every chart account -- active, untouched or inactive -- starts at
    zero and appears exactly once, in the chart's original order.
    Entries are visited in voucher-then-line order and all arithmetic
    stays in integer cents, so amounts beyond the float safe-integer
    range keep full precision.  The working slots never leave this
    call: callers only receive fresh frozen rows.
    """
    # Integer-cents [debit, credit] slots keyed by exact code.  Iterating
    # chart.accounts keeps chart order, and every account starts at zero
    # so unused accounts still report 0.00.
    slots: dict[str, list[int]] = {code: [0, 0] for code in chart.accounts}
    for _structured, body in validated:
        for entry in body.entries:
            slot = slots[entry.account_code]
            slot[0] += entry.debit_cents
            slot[1] += entry.credit_cents

    return tuple(
        AccountTurnover(account, slots[code][0], slots[code][1])
        for code, account in chart.accounts.items()
    )
