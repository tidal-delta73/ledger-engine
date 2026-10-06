"""Per-entry business validation against the parsed chart.

One structurally-valid entry in, one fully-resolved posting line out:
the chart account snapshot plus both amounts as integer cents.  Account
resolution (unknown, then inactive) always precedes amount parsing, and
the exactly-one-side rule closes the entry, matching the pipeline's
documented first-problem order.
"""
from __future__ import annotations

from ._amounts import parse_amount_cents
from ._errors import (
    InactiveAccountError,
    InvalidEntryAmountError,
    UnknownAccountError,
)


def validate_entry_business(
    entry: dict, accounts: dict[str, dict], *, where: str
) -> tuple[dict, int, int]:
    """Resolve account and amounts for one entry.

    Returns ``(account_snapshot, debit_cents, credit_cents)``.  Raises
    UnknownAccountError, InactiveAccountError or InvalidEntryAmountError,
    in that precedence, for the first defect found.
    """
    account_code = entry["account_code"]
    account = accounts.get(account_code)  # exact match, no normalization
    if account is None:
        raise UnknownAccountError(
            f"{where}: unknown account code {account_code!r}"
        )
    if not account["active"]:
        raise InactiveAccountError(
            f"{where}: account {account_code!r} is inactive"
        )

    debit_cents = parse_amount_cents(entry["debit"], where=where)
    credit_cents = parse_amount_cents(entry["credit"], where=where)
    if (debit_cents == 0) == (credit_cents == 0):
        raise InvalidEntryAmountError(
            f"{where}: exactly one of debit/credit must be non-zero"
        )
    return account, debit_cents, credit_cents
