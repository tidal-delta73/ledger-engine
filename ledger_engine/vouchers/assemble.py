"""Stage 3: construct the public result documents.

Only shape assembly lives here: field names, their insertion order and
the two-decimal rendering of integer-cents amounts.  No validation and
no arithmetic decisions happen in this stage, so every future consumer
(posting, reversal, recomputation) serializes validated entries through
the same deterministic builder and gets byte-comparable output.
"""
from __future__ import annotations

from .amounts import format_cents
from .entries import ValidatedEntry, ValidatedVoucherBody
from .structure import StructuredVoucher

__all__ = ["build_entry", "build_voucher"]

_ENTRY_FIELD_ORDER = (
    "line_no",
    "account_code",
    "account_name",
    "normal_side",
    "summary",
    "debit",
    "credit",
)
_VOUCHER_FIELD_ORDER = (
    "voucher_id",
    "date",
    "currency",
    "entries",
    "debit_total",
    "credit_total",
)


def build_entry(entry: ValidatedEntry) -> dict:
    """Assemble one public entry dict with a fixed key insertion order."""
    values = {
        "line_no": entry.line_no,
        "account_code": entry.account_code,
        "account_name": entry.account_name,
        "normal_side": entry.normal_side,
        "summary": entry.summary,
        "debit": format_cents(entry.debit_cents),
        "credit": format_cents(entry.credit_cents),
    }
    return {field: values[field] for field in _ENTRY_FIELD_ORDER}


def build_voucher(
    structured: StructuredVoucher, body: ValidatedVoucherBody
) -> dict:
    """Assemble one public voucher dict with a fixed key insertion order."""
    values = {
        "voucher_id": structured.voucher_id,
        "date": structured.date,
        "currency": structured.currency,
        "entries": [build_entry(entry) for entry in body.entries],
        "debit_total": format_cents(body.debit_total),
        "credit_total": format_cents(body.credit_total),
    }
    return {field: values[field] for field in _VOUCHER_FIELD_ORDER}
