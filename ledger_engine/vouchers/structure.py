"""Stage 1: voucher structural validation.

This stage enforces *shape only*: container types, required fields,
string/date/list types.  It never looks at the chart and never parses an
amount, so an unknown account or bad amount cannot mask a structural
defect (and vice versa).  The validated raw values are returned as
immutable records; later stages work on those records, not on caller
dicts, which keeps the input objects untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

from .errors import VoucherFormatError

__all__ = [
    "StructuredEntry",
    "StructuredVoucher",
    "require_voucher_list",
    "structure_voucher",
]

_VOUCHER_FIELDS = ("voucher_id", "date", "currency", "entries")
_ENTRY_FIELDS = ("account_code", "summary", "debit", "credit")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _valid_calendar_date(value: str) -> bool:
    # ASCII digits only (str.isdigit would admit Unicode numerals); then a
    # real calendar construction rejects impossible months/days.
    if _DATE_RE.fullmatch(value) is None:
        return False
    try:
        date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class StructuredEntry:
    """Raw entry values after structural validation (amounts still raw)."""

    account_code: str
    summary: str
    debit_text: str
    credit_text: str


@dataclass(frozen=True)
class StructuredVoucher:
    """Raw voucher values after structural validation."""

    voucher_id: str
    date: str
    currency: str
    entries: tuple[StructuredEntry, ...]


def require_voucher_list(vouchers: object) -> list:
    """The batch itself must be a list (checked after chart parsing)."""
    if not isinstance(vouchers, list):
        raise VoucherFormatError("vouchers must be a list")
    return vouchers


def structure_voucher(voucher: object, v_index: int) -> StructuredVoucher:
    """Run every structural check for one voucher and snapshot its values.

    All structural checks for the voucher (including every entry) finish
    here, before duplicate/currency/business checks begin.
    """
    where = f"voucher #{v_index}"
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

    structured_entries: list[StructuredEntry] = []
    for e_index, entry in enumerate(entries):
        entry_where = f"{where} ({voucher_id}) entry #{e_index}"
        if not isinstance(entry, dict):
            raise VoucherFormatError(f"{entry_where}: entry must be an object")
        for field in _ENTRY_FIELDS:
            if field not in entry:
                raise VoucherFormatError(
                    f"{entry_where}: missing field {field!r}"
                )
        account_code = entry["account_code"]
        summary = entry["summary"]
        if not isinstance(account_code, str):
            raise VoucherFormatError(
                f"{entry_where}: account_code must be a string, "
                f"got {account_code!r}"
            )
        if not isinstance(summary, str):
            raise VoucherFormatError(
                f"{entry_where}: summary must be a string, "
                f"got {summary!r}"
            )
        structured_entries.append(
            StructuredEntry(
                account_code=account_code,
                summary=summary,
                debit_text=entry["debit"],
                credit_text=entry["credit"],
            )
        )

    return StructuredVoucher(
        voucher_id=voucher_id,
        date=date_text,
        currency=currency,
        entries=tuple(structured_entries),
    )
