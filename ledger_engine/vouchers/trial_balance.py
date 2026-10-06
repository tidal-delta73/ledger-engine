"""Trial balance: validated vouchers -> reproducible trial balance.

The public :func:`build_trial_balance` consumes the same validated batch
as :func:`ledger_engine.vouchers.pipeline.normalize_vouchers` and
:func:`ledger_engine.vouchers.posting.post_vouchers`, so the validation
order, exception categories, exact-match account resolution and
integer-cents arithmetic are shared, not copied.  Only shape assembly
lives here: one row per chart account in original chart order, and a
deterministic zero-value totals summary for empty batches and empty
charts.  Every amount is rendered with exactly two decimal places via
the shared fixed-point primitives; inputs are never mutated and repeated
calls return equal, brand-new containers.
"""
from __future__ import annotations

from .amounts import format_cents
from .pipeline import _validate_batch

__all__ = ["build_trial_balance"]

_ACCOUNT_FIELD_ORDER = (
    "code",
    "name",
    "normal_side",
    "debit_turnover",
    "credit_turnover",
    "ending_debit",
    "ending_credit",
)
_TOTALS_FIELD_ORDER = (
    "debit_turnover",
    "credit_turnover",
    "ending_debit",
    "ending_credit",
    "turnover_balanced",
    "ending_balanced",
)


def _build_account_row(account, debit_cents: int, credit_cents: int) -> dict:
    """Assemble one trial-balance row from integer-cents turnovers.

    The ending net is taken as debit turnover minus credit turnover and
    lands on exactly one side: a positive net goes to ``ending_debit``,
    a negative net's absolute value goes to ``ending_credit``, and zero
    leaves both ending columns at ``0.00``.
    """
    net = debit_cents - credit_cents
    if net > 0:
        ending_debit_cents = net
        ending_credit_cents = 0
    elif net < 0:
        ending_debit_cents = 0
        ending_credit_cents = -net
    else:
        ending_debit_cents = 0
        ending_credit_cents = 0
    values = {
        "code": account.code,
        "name": account.name,
        "normal_side": account.normal_side,
        "debit_turnover": format_cents(debit_cents),
        "credit_turnover": format_cents(credit_cents),
        "ending_debit": format_cents(ending_debit_cents),
        "ending_credit": format_cents(ending_credit_cents),
    }
    return {field: values[field] for field in _ACCOUNT_FIELD_ORDER}


def build_trial_balance(
    base_currency: object,
    chart_of_accounts: object,
    vouchers: object,
) -> dict:
    """Validate a raw voucher batch and build its trial balance.

    Accepts the same arguments as :func:`normalize_vouchers` and fails
    with the same first leaf exception on any problem; nothing partial is
    returned and the inputs are left untouched.  On success returns a new
    JSON-serializable dict with ``base_currency``, ``accounts`` (one row
    per chart account, in chart order, including untouched and inactive
    accounts with all-zero amounts) and ``totals`` (sums of the four
    amount columns plus the ``turnover_balanced``/``ending_balanced``
    flags).  All arithmetic stays in integer cents; empty batches and
    empty charts yield deterministic zero totals.
    """
    chart, validated = _validate_batch(
        base_currency, chart_of_accounts, vouchers
    )

    # Integer-cents [debit turnover, credit turnover] keyed by exact code;
    # every chart account starts at zero so unused accounts still report 0.00.
    turnovers: dict[str, list[int]] = {
        code: [0, 0] for code in chart.accounts
    }
    for _, body in validated:
        for entry in body.entries:
            totals = turnovers[entry.account_code]
            totals[0] += entry.debit_cents
            totals[1] += entry.credit_cents

    accounts = []
    total_debit_turnover = 0
    total_credit_turnover = 0
    total_ending_debit = 0
    total_ending_credit = 0
    for account in chart.accounts.values():
        debit_cents, credit_cents = turnovers[account.code]
        row = _build_account_row(account, debit_cents, credit_cents)
        accounts.append(row)
        total_debit_turnover += debit_cents
        total_credit_turnover += credit_cents
        net = debit_cents - credit_cents
        if net > 0:
            total_ending_debit += net
        elif net < 0:
            total_ending_credit += -net

    totals_values = {
        "debit_turnover": format_cents(total_debit_turnover),
        "credit_turnover": format_cents(total_credit_turnover),
        "ending_debit": format_cents(total_ending_debit),
        "ending_credit": format_cents(total_ending_credit),
        "turnover_balanced": total_debit_turnover == total_credit_turnover,
        "ending_balanced": total_ending_debit == total_ending_credit,
    }
    totals = {field: totals_values[field] for field in _TOTALS_FIELD_ORDER}

    return {
        "base_currency": chart.base_currency,
        "accounts": accounts,
        "totals": totals,
    }
