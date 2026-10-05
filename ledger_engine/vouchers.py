"""Validation and normalization of raw double-entry vouchers.

The single public entry point is :func:`normalize_vouchers`.  It is a pure
function: inputs are never mutated, nothing is written to disk, and no global
state is kept between calls.
"""
import datetime
import re
from decimal import Decimal

from .errors import (
    ChartOfAccountsError,
    DuplicateVoucherError,
    InactiveAccountError,
    InvalidEntryAmountError,
    UnbalancedVoucherError,
    UnsupportedCurrencyError,
    UnknownAccountError,
    VoucherFormatError,
)

_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_CENTS = Decimal("0.01")

_ACCOUNT_FIELDS = ("code", "name", "normal_side", "active")
_VOUCHER_FIELDS = ("voucher_id", "date", "currency", "entries")
_ENTRY_FIELDS = ("account_code", "summary", "debit", "credit")


def _is_str(value) -> bool:
    # bool is not a string; no scalar types need excluding beyond that.
    return isinstance(value, str)


def _parse_amount(value) -> Decimal:
    """Return the Decimal value of a fixed-point amount string.

    Only decimal fixed-point strings are accepted: no sign, no exponent, no
    thousands separator, at most two fractional digits.  Anything else
    (including non-strings, "", NaN and Infinity) is an invalid amount.
    """
    if not isinstance(value, str) or not _AMOUNT_RE.fullmatch(value):
        raise InvalidEntryAmountError(f"invalid amount: {value!r}")
    return Decimal(value)


def _build_accounts(chart_of_accounts) -> dict:
    if not isinstance(chart_of_accounts, list):
        raise ChartOfAccountsError("chart of accounts must be a list")
    accounts: dict[str, dict] = {}
    for index, account in enumerate(chart_of_accounts):
        where = f"account at index {index}"
        if not isinstance(account, dict):
            raise ChartOfAccountsError(f"{where} must be an object")
        for field in _ACCOUNT_FIELDS:
            if field not in account:
                raise ChartOfAccountsError(f"{where} missing field {field!r}")
        code = account["code"]
        name = account["name"]
        normal_side = account["normal_side"]
        active = account["active"]
        if not _is_str(code):
            raise ChartOfAccountsError(f"{where} field 'code' must be a string")
        if not _is_str(name):
            raise ChartOfAccountsError(f"{where} field 'name' must be a string")
        if not _is_str(normal_side) or normal_side not in ("debit", "credit"):
            raise ChartOfAccountsError(
                f"{where} field 'normal_side' must be 'debit' or 'credit'"
            )
        if not isinstance(active, bool):
            raise ChartOfAccountsError(f"{where} field 'active' must be a boolean")
        if code in accounts:
            raise ChartOfAccountsError(f"duplicate account code: {code!r}")
        accounts[code] = {
            "code": code,
            "name": name,
            "normal_side": normal_side,
            "active": active,
        }
    return accounts


def _parse_date(value) -> None:
    if not isinstance(value, str):
        raise VoucherFormatError("voucher field 'date' must be a string")
    match = _DATE_RE.fullmatch(value)
    if match is None:
        raise VoucherFormatError(f"invalid date: {value!r}")
    try:
        datetime.date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError as exc:
        raise VoucherFormatError(f"invalid date: {value!r}") from exc


def normalize_vouchers(functional_currency, chart_of_accounts, vouchers):
    """Validate and normalize raw vouchers, preserving input order.

    Validation stops at the first problem encountered; either every voucher is
    returned normalized, or an error is raised and no partial result is given.

    See :mod:`ledger_engine.errors` for the exception types.
    """
    if not isinstance(functional_currency, str) or not _CURRENCY_RE.fullmatch(
        functional_currency
    ):
        raise ChartOfAccountsError(
            f"functional currency must be three uppercase letters: "
            f"{functional_currency!r}"
        )

    accounts = _build_accounts(chart_of_accounts)

    if not isinstance(vouchers, list):
        raise VoucherFormatError("vouchers must be a list")

    seen_ids: set[str] = set()
    normalized = []

    for v_index, voucher in enumerate(vouchers):
        where = f"voucher at index {v_index}"
        if not isinstance(voucher, dict):
            raise VoucherFormatError(f"{where} must be an object")
        for field in _VOUCHER_FIELDS:
            if field not in voucher:
                raise VoucherFormatError(f"{where} missing field {field!r}")

        voucher_id = voucher["voucher_id"]
        date = voucher["date"]
        currency = voucher["currency"]
        entries = voucher["entries"]

        if not _is_str(voucher_id):
            raise VoucherFormatError(f"{where} field 'voucher_id' must be a string")
        _parse_date(date)
        if not _is_str(currency):
            raise VoucherFormatError(f"{where} field 'currency' must be a string")
        if not isinstance(entries, list):
            raise VoucherFormatError(f"{where} field 'entries' must be a list")
        if len(entries) < 2:
            raise VoucherFormatError(
                f"{where} must contain at least two entries, got {len(entries)}"
            )

        if voucher_id in seen_ids:
            raise DuplicateVoucherError(f"duplicate voucher_id: {voucher_id!r}")
        seen_ids.add(voucher_id)

        if currency != functional_currency:
            raise UnsupportedCurrencyError(
                f"voucher {voucher_id!r} currency {currency!r} is not the "
                f"functional currency {functional_currency!r}"
            )

        normalized_entries = []
        debit_total = Decimal("0")
        credit_total = Decimal("0")

        for e_index, entry in enumerate(entries):
            entry_where = f"entry index {e_index} of voucher {voucher_id!r}"
            if not isinstance(entry, dict):
                raise VoucherFormatError(f"{entry_where} must be an object")
            for field in _ENTRY_FIELDS:
                if field not in entry:
                    raise VoucherFormatError(
                        f"{entry_where} missing field {field!r}"
                    )

            account_code = entry["account_code"]
            summary = entry["summary"]
            if not _is_str(account_code):
                raise VoucherFormatError(
                    f"{entry_where} field 'account_code' must be a string"
                )
            if not _is_str(summary):
                raise VoucherFormatError(
                    f"{entry_where} field 'summary' must be a string"
                )

            debit = _parse_amount(entry["debit"])
            credit = _parse_amount(entry["credit"])
            debit_nonzero = debit != 0
            credit_nonzero = credit != 0
            if debit_nonzero == credit_nonzero:
                # Either both sides are nonzero or both are zero.
                raise InvalidEntryAmountError(
                    f"{entry_where} must have exactly one nonzero side "
                    f"(debit={entry['debit']!r}, credit={entry['credit']!r})"
                )

            account = accounts.get(account_code)
            if account is None:
                raise UnknownAccountError(
                    f"{entry_where} references unknown account {account_code!r}"
                )
            if not account["active"]:
                raise InactiveAccountError(
                    f"{entry_where} references inactive account {account_code!r}"
                )

            if debit_nonzero:
                debit_total += debit
            else:
                credit_total += credit

            normalized_entries.append(
                {
                    "line_no": e_index + 1,
                    "account_code": account_code,
                    "account_name": account["name"],
                    "normal_side": account["normal_side"],
                    "summary": summary,
                    "debit": str(debit.quantize(_CENTS)),
                    "credit": str(credit.quantize(_CENTS)),
                }
            )

        if debit_total != credit_total:
            raise UnbalancedVoucherError(
                f"voucher {voucher_id!r} is unbalanced: "
                f"debit total {debit_total} != credit total {credit_total}"
            )

        normalized.append(
            {
                "voucher_id": voucher_id,
                "date": date,
                "currency": currency,
                "entries": normalized_entries,
                "debit_total": str(debit_total.quantize(_CENTS)),
                "credit_total": str(credit_total.quantize(_CENTS)),
            }
        )

    return normalized
