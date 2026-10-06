"""Posting: validated vouchers -> reproducible journal and balances.

The public :func:`post_vouchers` consumes the same validated batch as
:func:`ledger_engine.vouchers.pipeline.normalize_vouchers`, so the
validation order, exception categories, exact-match account resolution
and integer-cents arithmetic are shared, not copied.  Per-account
debit/credit turnovers come from the single internal aggregation
:func:`ledger_engine.vouchers.ledger.accumulate_turnovers`, shared with
the trial balance; only posting-specific shape assembly lives here:
journal rows in voucher-then-line order, and one summary row per chart
account in original chart order with the ``normal_side`` ending rule.
Every amount is rendered with exactly two decimal places via the shared
fixed-point primitives; inputs are never mutated and repeated calls
return equal, brand-new containers.
"""
from __future__ import annotations

from .amounts import format_cents
from .ledger import accumulate_turnovers
from .pipeline import _validate_batch

__all__ = ["post_vouchers"]

_JOURNAL_FIELD_ORDER = (
    "voucher_id",
    "date",
    "line_no",
    "account_code",
    "account_name",
    "summary",
    "debit",
    "credit",
)
_ACCOUNT_FIELD_ORDER = (
    "code",
    "name",
    "normal_side",
    "debit_turnover",
    "credit_turnover",
    "ending_side",
    "ending_balance",
)

_OPPOSITE_SIDE = {"debit": "credit", "credit": "debit"}


def _build_journal_row(structured, entry) -> dict:
    """Assemble one journal row with a fixed key insertion order."""
    values = {
        "voucher_id": structured.voucher_id,
        "date": structured.date,
        "line_no": entry.line_no,
        "account_code": entry.account_code,
        "account_name": entry.account_name,
        "summary": entry.summary,
        "debit": format_cents(entry.debit_cents),
        "credit": format_cents(entry.credit_cents),
    }
    return {field: values[field] for field in _JOURNAL_FIELD_ORDER}


def _build_account_row(turnover) -> dict:
    """Assemble one account summary row from a shared turnover fact.

    The net is taken in the account's normal direction; a negative net
    flips ``ending_side`` to the opposite side with the absolute value,
    and a zero net keeps ``normal_side``.
    """
    account = turnover.account
    debit_cents = turnover.debit_cents
    credit_cents = turnover.credit_cents
    if account.normal_side == "debit":
        net = debit_cents - credit_cents
    else:
        net = credit_cents - debit_cents
    if net < 0:
        ending_side = _OPPOSITE_SIDE[account.normal_side]
        ending_cents = -net
    else:
        ending_side = account.normal_side
        ending_cents = net
    values = {
        "code": account.code,
        "name": account.name,
        "normal_side": account.normal_side,
        "debit_turnover": format_cents(debit_cents),
        "credit_turnover": format_cents(credit_cents),
        "ending_side": ending_side,
        "ending_balance": format_cents(ending_cents),
    }
    return {field: values[field] for field in _ACCOUNT_FIELD_ORDER}


def post_vouchers(
    base_currency: object,
    chart_of_accounts: object,
    vouchers: object,
) -> dict:
    """Validate a raw voucher batch and post it to a journal + balances.

    Accepts the same arguments as :func:`normalize_vouchers` and fails
    with the same first leaf exception on any problem; nothing partial
    is returned.  On success returns a new JSON-serializable dict with
    ``base_currency``, ``journal`` (one row per entry, in voucher and
    line order, nothing merged) and ``accounts`` (one row per chart
    account, in chart order, including untouched and inactive accounts
    with zero turnovers and balances).
    """
    chart, validated = _validate_batch(
        base_currency, chart_of_accounts, vouchers
    )

    journal: list[dict] = []
    for structured, body in validated:
        for entry in body.entries:
            journal.append(_build_journal_row(structured, entry))

    # Turnovers are the single shared accumulation fact; posting only
    # layers its normal_side ending presentation on top.
    accounts = [
        _build_account_row(turnover)
        for turnover in accumulate_turnovers(chart, validated)
    ]

    return {
        "base_currency": chart.base_currency,
        "journal": journal,
        "accounts": accounts,
    }
