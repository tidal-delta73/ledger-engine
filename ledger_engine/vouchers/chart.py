"""Chart-of-accounts parsing: raw payload -> frozen account snapshot.

This is the single place that decides what an account is and how account
codes resolve.  Later voucher stages receive an immutable
:class:`Chart`; they never inspect raw chart dicts, so posting,
reversals and recomputation reuse exactly the same exact-match lookup,
active-flag and name/normal-side snapshot rules.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Mapping

from .errors import ChartOfAccountsError

__all__ = ["Account", "Chart", "validate_base_currency", "parse_chart"]

_BASE_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_ACCOUNT_FIELDS = ("code", "name", "normal_side", "active")


@dataclass(frozen=True)
class Account:
    """Immutable snapshot of one chart row.

    The values are copied out of the raw payload at parse time; mutating
    the caller's chart afterwards cannot affect normalization.
    """

    code: str
    name: str
    normal_side: str  # "debit" | "credit"
    active: bool


@dataclass(frozen=True)
class Chart:
    """Immutable account table keyed by exact account code."""

    base_currency: str
    accounts: Mapping[str, Account]

    def get(self, code: str) -> Account | None:
        """Exact-match lookup; no case folding, trimming or conversion."""
        return self.accounts.get(code)


def validate_base_currency(base_currency: object) -> str:
    """Validate the single bookkeeping base currency; return it as-is."""
    if not isinstance(base_currency, str) or _BASE_CURRENCY_RE.fullmatch(
        base_currency
    ) is None:
        raise ChartOfAccountsError(
            "base currency must be exactly three uppercase letters A-Z, "
            f"got {base_currency!r}"
        )
    return base_currency


def parse_chart(base_currency: object, chart: object) -> Chart:
    """Validate base currency then the raw chart list.

    Returns an immutable snapshot whose fields are copies of the raw
    values.  Field order, error wording and the duplicate-code boundary
    mirror the public contract.
    """
    currency = validate_base_currency(base_currency)
    if not isinstance(chart, list):
        raise ChartOfAccountsError("chart of accounts must be a list")
    accounts: dict[str, Account] = {}
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
        accounts[code] = Account(
            code=code, name=name, normal_side=normal_side, active=active
        )
    return Chart(base_currency=currency, accounts=MappingProxyType(accounts))
