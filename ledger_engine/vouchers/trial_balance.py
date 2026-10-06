"""Trial balance: validated vouchers -> a reproducible trial balance.

The public :func:`build_trial_balance` consumes the same validated batch
as :func:`ledger_engine.vouchers.pipeline.normalize_vouchers` and
:func:`ledger_engine.vouchers.posting.post_vouchers`, so the validation
order, exception categories, exact-match account resolution and
integer-cents arithmetic are shared, not copied.  Only shape assembly
lives here: one row per chart account in original chart order, and a
single totals summary.  The ending net always takes the debit-minus-
credit direction, independent of an account's ``normal_side``; a zero
net reports ``0.00`` on both sides.  Every amount is rendered with
exactly two decimal places via the shared fixed-point primitives;
inputs are never mutated and repeated calls return equal, brand-new
containers.
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

    The net is debit turnover minus credit turnover, regardless of the
    account's normal side: a positive net lands entirely in
    ``ending_debit``, a negative net as its absolute value in
    ``ending_credit``, and a zero net leaves both columns at ``0.00``.
    """
    net = debit_cents - credit_cents
    if net > 0:
        ending_debit_cents, ending_credit_cents = net, 0
    elif net < 0:
        ending_debit_cents, ending_credit_cents = 0, -net
    else:
        ending_debit_cents = ending_credit_cents = 0
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
    with the same first leaf exception on any problem; nothing partial
    is returned.  On success returns a new JSON-serializable dict with
    ``base_currency``, ``accounts`` (one row per chart account, in chart
    order, including untouched and inactive accounts with all-zero
    amounts) and ``totals`` (the four amount columns summed plus the
    ``turnover_balanced``/``ending_balanced`` flags).  All arithmetic
    stays in integer cents, so arbitrarily large amounts never pass
    through a float.
    """
    chart, validated = _validate_batch(
        base_currency, chart_of_accounts, vouchers
    )

    # Integer-cents [debit, credit] turnovers keyed by exact code; every
    # chart account starts at zero so unused accounts still report 0.00.
    turnovers: dict[str, list[int]] = {
        code: [0, 0] for code in chart.accounts
    }
    for _structured, body in validated:
        for entry in body.entries:
            totals = turnovers[entry.account_code]
            totals[0] += entry.debit_cents
            totals[1] += entry.credit_cents

    accounts: list[dict] = []
    total_debit = total_credit = 0
    total_ending_debit = total_ending_credit = 0
    for account in chart.accounts.values():
        debit_cents, credit_cents = turnovers[account.code]
        accounts.append(
            _build_account_row(account, debit_cents, credit_cents)
        )
        total_debit += debit_cents
        total_credit += credit_cents
        net = debit_cents - credit_cents
        if net > 0:
            total_ending_debit += net
        elif net < 0:
            total_ending_credit += -net

    values = {
        "debit_turnover": format_cents(total_debit),
        "credit_turnover": format_cents(total_credit),
        "ending_debit": format_cents(total_ending_debit),
        "ending_credit": format_cents(total_ending_credit),
        "turnover_balanced": total_debit == total_credit,
        "ending_balanced": total_ending_debit == total_ending_credit,
    }
    totals = {field: values[field] for field in _TOTALS_FIELD_ORDER}

    return {
        "base_currency": chart.base_currency,
        "accounts": accounts,
        "totals": totals,
    }
