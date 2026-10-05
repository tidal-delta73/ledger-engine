"""Validation and normalization of raw double-entry vouchers.

Pure-Python, no runtime dependencies, no global state. Inputs are never
mutated; on success a new list of JSON-serializable dicts is returned in
the same order. Validation stops at the first problem encountered.
"""
from __future__ import annotations

import re
from datetime import date

_BASE_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Fixed-point decimal only: digits, optional one dot with 1-2 fraction digits.
# No sign, exponent, thousands separator, whitespace or leading dot.
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")

_ACCOUNT_FIELDS = ("code", "name", "normal_side", "active")
_VOUCHER_FIELDS = ("voucher_id", "date", "currency", "entries")
_ENTRY_FIELDS = ("account_code", "summary", "debit", "credit")


class LedgerEngineError(Exception):
    """Base class for all ledger_engine validation errors."""


class ChartOfAccountsError(LedgerEngineError):
    """Base currency is not three uppercase letters or the chart is invalid."""


class VoucherFormatError(LedgerEngineError):
    """A voucher or entry has invalid/missing fields or wrong types."""


class DuplicateVoucherError(LedgerEngineError):
    """Two vouchers in the same batch share a voucher_id (exact match)."""


class UnsupportedCurrencyError(LedgerEngineError):
    """A voucher currency does not equal the base currency."""


class UnknownAccountError(LedgerEngineError):
    """An entry references an account code absent from the chart."""


class InactiveAccountError(LedgerEngineError):
    """An entry references an account marked inactive."""


class InvalidEntryAmountError(LedgerEngineError):
    """An amount is not a valid fixed-point string, or an entry is not
    exactly one-sided."""


class UnbalancedVoucherError(LedgerEngineError):
    """A voucher's debit and credit totals differ."""


def _parse_amount(value: object, *, where: str) -> int:
    """Return the amount in integer cents; raise InvalidEntryAmountError."""
    if not isinstance(value, str) or _AMOUNT_RE.fullmatch(value) is None:
        raise InvalidEntryAmountError(f"{where}: amount must be a fixed-point "
                                     f"decimal string with at most two places, "
                                     f"got {value!r}")
    if "." in value:
        whole, frac = value.split(".", 1)
    else:
        whole, frac = value, ""
    return int(whole) * 100 + int((frac + "00")[:2])


def _format_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _valid_calendar_date(value: str) -> bool:
    if _DATE_RE.fullmatch(value) is None:
        return False
    try:
        date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return False
    return True


def _validate_base_currency(base_currency: object) -> None:
    if not isinstance(base_currency, str) or _BASE_CURRENCY_RE.fullmatch(
        base_currency
    ) is None:
        raise ChartOfAccountsError(
            "base currency must be exactly three uppercase letters A-Z, "
            f"got {base_currency!r}"
        )


def _validate_chart(chart: object) -> dict[str, dict]:
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


def normalize_vouchers(
    base_currency: object,
    chart_of_accounts: object,
    vouchers: object,
) -> list[dict]:
    """Validate and normalize a batch of raw vouchers.

    Checks run in input order and only the first problem is reported.
    Returns a new list (inputs are untouched). Every amount is rendered
    as a string with exactly two decimal places and entries get 1-based
    line numbers.
    """
    _validate_base_currency(base_currency)
    accounts = _validate_chart(chart_of_accounts)
    if not isinstance(vouchers, list):
        raise VoucherFormatError("vouchers must be a list")

    seen_ids: set[str] = set()
    results: list[dict] = []

    for v_index, voucher in enumerate(vouchers):
        where = f"voucher #{v_index}"

        # --- Phase 1: structural/format checks (VoucherFormatError) ---
        if not isinstance(voucher, dict):
            raise VoucherFormatError(f"{where}: voucher must be an object")
        for field in _VOUCHER_FIELDS:
            if field not in voucher:
                raise VoucherFormatError(f"{where}: missing field {field!r}")
        voucher_id = voucher["voucher_id"]
        date_text = voucher["date"]
        currency = voucher["currency"]
        entries = voucher["entries"]

        if not isinstance(voucher_id, str) or voucher_id == "":
            raise VoucherFormatError(
                f"{where}: voucher_id must be a non-empty string, "
                f"got {voucher_id!r}"
            )
        if not isinstance(date_text, str) or not _valid_calendar_date(date_text):
            raise VoucherFormatError(
                f"{where} ({voucher_id}): date must be a valid YYYY-MM-DD "
                f"calendar date, got {date_text!r}"
            )
        if not isinstance(currency, str):
            raise VoucherFormatError(
                f"{where} ({voucher_id}): currency must be a string, "
                f"got {currency!r}"
            )
        if not isinstance(entries, list) or len(entries) < 2:
            raise VoucherFormatError(
                f"{where} ({voucher_id}): entries must be a list of at least "
                f"two entries"
            )
        for e_index, entry in enumerate(entries):
            entry_where = f"{where} ({voucher_id}) entry #{e_index}"
            if not isinstance(entry, dict):
                raise VoucherFormatError(f"{entry_where}: entry must be an object")
            for field in _ENTRY_FIELDS:
                if field not in entry:
                    raise VoucherFormatError(
                        f"{entry_where}: missing field {field!r}"
                    )
            if not isinstance(entry["account_code"], str):
                raise VoucherFormatError(
                    f"{entry_where}: account_code must be a string, "
                    f"got {entry['account_code']!r}"
                )
            if not isinstance(entry["summary"], str):
                raise VoucherFormatError(
                    f"{entry_where}: summary must be a string, "
                    f"got {entry['summary']!r}"
                )

        # --- Phase 2: batch and currency checks ---
        if voucher_id in seen_ids:
            raise DuplicateVoucherError(
                f"{where}: duplicate voucher_id {voucher_id!r}"
            )
        seen_ids.add(voucher_id)
        if currency != base_currency:
            raise UnsupportedCurrencyError(
                f"{where} ({voucher_id}): currency {currency!r} does not match "
                f"base currency {base_currency!r}"
            )

        # --- Phase 3: entries (accounts, then amounts), in entry order ---
        normalized_entries: list[dict] = []
        debit_total = 0
        credit_total = 0
        for e_index, entry in enumerate(entries):
            entry_where = f"{where} ({voucher_id}) line {e_index + 1}"
            account_code = entry["account_code"]
            account = accounts.get(account_code)  # exact match, no normalization
            if account is None:
                raise UnknownAccountError(
                    f"{entry_where}: unknown account code {account_code!r}"
                )
            if not account["active"]:
                raise InactiveAccountError(
                    f"{entry_where}: account {account_code!r} is inactive"
                )

            debit_cents = _parse_amount(entry["debit"], where=entry_where)
            credit_cents = _parse_amount(entry["credit"], where=entry_where)
            if (debit_cents == 0) == (credit_cents == 0):
                raise InvalidEntryAmountError(
                    f"{entry_where}: exactly one of debit/credit must be "
                    f"non-zero"
                )

            debit_total += debit_cents
            credit_total += credit_cents
            normalized_entries.append(
                {
                    "line_no": e_index + 1,
                    "account_code": account["code"],
                    "account_name": account["name"],
                    "normal_side": account["normal_side"],
                    "summary": entry["summary"],
                    "debit": _format_cents(debit_cents),
                    "credit": _format_cents(credit_cents),
                }
            )

        # --- Phase 4: balance ---
        if debit_total != credit_total:
            raise UnbalancedVoucherError(
                f"{where} ({voucher_id}): debit total "
                f"{_format_cents(debit_total)} != credit total "
                f"{_format_cents(credit_total)}"
            )

        results.append(
            {
                "voucher_id": voucher_id,
                "date": date_text,
                "currency": currency,
                "entries": normalized_entries,
                "debit_total": _format_cents(debit_total),
                "credit_total": _format_cents(credit_total),
            }
        )

    return results
