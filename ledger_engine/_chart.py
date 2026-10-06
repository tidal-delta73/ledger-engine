"""Base-currency validation and chart-of-accounts parsing.

The chart is validated once per call and reduced to an immutable-by-
convention lookup of account snapshots keyed by exact code.  No case
folding, trimming or any other normalization is applied to codes.
"""
from __future__ import annotations

import re

from ._errors import ChartOfAccountsError

_BASE_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_ACCOUNT_FIELDS = ("code", "name", "normal_side", "active")


def validate_base_currency(base_currency: object) -> None:
    """Raise ChartOfAccountsError unless the value is three A-Z letters."""
    if not isinstance(base_currency, str) or _BASE_CURRENCY_RE.fullmatch(
        base_currency
    ) is None:
        raise ChartOfAccountsError(
            "base currency must be exactly three uppercase letters A-Z, "
            f"got {base_currency!r}"
        )


def parse_chart(chart: object) -> dict[str, dict]:
    """Validate the raw chart and return ``{code: account snapshot}``.

    Each snapshot is a fresh dict holding only the four public fields, so
    later stages never alias the caller's objects.
    """
    if not isinstance(chart, list):
        raise ChartOfAccountsError("chart of accounts must be a list")
    accounts: dict[str, dict] = {}
    for index, item in enumerate(chart):
        where = f"account #{index}"
        if not isinstance(item, dict):
            raise ChartOfAccountsError(f"{where}: account must be an object")
        for field in _ACCOUNT_FIELDS:
            if field not in item:
                raise ChartOfAccountsError(f"{where}: missing field {field!r}")
        code = item["code"]
        name = item["name"]
        normal_side = item["normal_side"]
        active = item["active"]
        if not isinstance(code, str) or code == "":
            raise ChartOfAccountsError(
                f"{where}: code must be a non-empty string, got {code!r}"
            )
        if not isinstance(name, str) or name == "":
            raise ChartOfAccountsError(
                f"{where}: name must be a non-empty string, got {name!r}"
            )
        if normal_side not in ("debit", "credit"):
            raise ChartOfAccountsError(
                f"{where} ({code}): normal_side must be 'debit' or 'credit', "
                f"got {normal_side!r}"
            )
        if not isinstance(active, bool):
            raise ChartOfAccountsError(
                f"{where} ({code}): active must be a boolean, got {active!r}"
            )
        if code in accounts:
            raise ChartOfAccountsError(f"{where}: duplicate account code {code!r}")
        accounts[code] = {
            "code": code,
            "name": name,
            "normal_side": normal_side,
            "active": active,
        }
    return accounts
