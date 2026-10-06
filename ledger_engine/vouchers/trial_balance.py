"""Trial balance: validated vouchers -> a reproducible trial balance.

The public :func:`build_trial_balance` consumes the same validated batch
as :func:`ledger_engine.vouchers.pipeline.normalize_vouchers` and
:func:`ledger_engine.vouchers.posting.post_vouchers`, so the validation
order, exception categories, exact-match account resolution and
integer-cents arithmetic are shared, not copied.  Per-account debit/
credit turnovers come from the single internal aggregation
:func:`ledger_engine.vouchers.ledger.accumulate_turnovers`, shared with
posting.  Only trial-balance shape assembly lives here: one row per
chart account in original chart order, and a single totals summary.
The ending net always takes the debit-minus-credit direction,
independent of an account's ``normal_side``; a zero net reports
``0.00`` on both sides.  Every amount is rendered with exactly two
decimal places via the shared fixed-point primitives; inputs are never
mutated and repeated calls return equal, brand-new containers.
"""
from __future__ import annotations

from .amounts import format_cents
from .ledger import AccountTurnover, accumulate_turnovers
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


def _build_account_row(turnover: AccountTurnover) -> dict:
    """Assemble one trial-balance row from a shared turnover fact.

    The net is debit turnover minus credit turnover, regardless of the
    account's normal side: a positive net lands entirely in
    ``ending_debit``, a negative net as its absolute value in
    ``ending_credit``, and a zero net leaves both columns at ``0.00``.
    """
    account = turnover.account
    debit_cents = turnover.debit_cents
    credit_cents = turnover.credit_cents
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


def _build_totals(turnovers: tuple[AccountTurnover, ...]) -> dict:
    """Sum the four amount columns over the shared turnovers."""
    total_debit = total_credit = 0
    total_ending_debit = total_ending_credit = 0
    for turnover in turnovers:
        total_debit += turnover.debit_cents
        total_credit += turnover.credit_cents
        net = turnover.debit_cents - turnover.credit_cents
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
    return {field: values[field] for field in _TOTALS_FIELD_ORDER}


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

    # The single shared accumulation fact; the trial balance only layers
    # its debit-minus-credit rows and the totals summary on top.
    turnovers = accumulate_turnovers(chart, validated)
    accounts = [_build_account_row(turnover) for turnover in turnovers]
    totals = _build_totals(turnovers)

    return {
        "base_currency": chart.base_currency,
        "accounts": accounts,
        "totals": totals,
    }
