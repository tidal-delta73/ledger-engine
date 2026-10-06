"""Posting: validated vouchers -> reproducible journal and balances.

The public :func:`post_vouchers` consumes the same validated batch as
:func:`ledger_engine.vouchers.pipeline.normalize_vouchers`, so the
validation order, exception categories, exact-match account resolution
and integer-cents arithmetic are shared, not copied.  Only shape
assembly lives here: journal rows in voucher-then-line order, and one
summary row per chart account in original chart order.  Every amount
is rendered with exactly two decimal places via the shared fixed-point
primitives; inputs are never mutated and repeated calls return equal,
brand-new containers.
"""
from __future__ import annotations

from .amounts import format_cents
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


def _build_account_row(account, debit_cents: int, credit_cents: int) -> dict:
    """Assemble one account summary row from integer-cents turnovers.

    The net is taken in the account's normal direction; a negative net
    flips ``ending_side`` to the opposite side with the absolute value,
    and a zero net keeps ``normal_side``.
    """
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
    # Integer-cents [debit, credit] turnovers keyed by exact code; every
    # chart account starts at zero so unused accounts still report 0.00.
    turnovers: dict[str, list[int]] = {
        code: [0, 0] for code in chart.accounts
    }
    for structured, body in validated:
        for entry in body.entries:
            journal.append(_build_journal_row(structured, entry))
            totals = turnovers[entry.account_code]
            totals[0] += entry.debit_cents
            totals[1] += entry.credit_cents

    accounts = [
        _build_account_row(account, *turnovers[account.code])
        for account in chart.accounts.values()
    ]

    return {
        "base_currency": chart.base_currency,
        "journal": journal,
        "accounts": accounts,
    }
