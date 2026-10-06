"""Structural validation of raw vouchers and their entries.

This stage checks shapes and types only: required fields, string types,
calendar-date well-formedness and the minimum entry count.  Every
structural check for one voucher (including all of its entries) runs
before any business rule for that voucher is considered.
"""
from __future__ import annotations

import re
from datetime import date

from ._errors import VoucherFormatError

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

_VOUCHER_FIELDS = ("voucher_id", "date", "currency", "entries")
_ENTRY_FIELDS = ("account_code", "summary", "debit", "credit")


def _valid_calendar_date(value: str) -> bool:
    if _DATE_RE.fullmatch(value) is None:
        return False
    try:
        date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return False
    return True


def _validate_entry_structure(entry: object, *, where: str) -> None:
    if not isinstance(entry, dict):
        raise VoucherFormatError(f"{where}: entry must be an object")
    for field in _ENTRY_FIELDS:
        if field not in entry:
            raise VoucherFormatError(f"{where}: missing field {field!r}")
    if not isinstance(entry["account_code"], str):
        raise VoucherFormatError(
            f"{where}: account_code must be a string, "
            f"got {entry['account_code']!r}"
        )
    if not isinstance(entry["summary"], str):
        raise VoucherFormatError(
            f"{where}: summary must be a string, "
            f"got {entry['summary']!r}"
        )


def validate_voucher_structure(
    voucher: object, *, where: str
) -> tuple[str, str, str, list]:
    """Check one raw voucher's shape; return its four public fields.

    Raises VoucherFormatError on the first structural defect.  The
    returned ``entries`` list is the caller's own object (read-only use);
    nothing is copied or mutated here.
    """
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
        _validate_entry_structure(
            entry, where=f"{where} ({voucher_id}) entry #{e_index}"
        )
    return voucher_id, date_text, currency, entries
