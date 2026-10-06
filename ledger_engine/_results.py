"""Construction of normalized, JSON-serializable result documents.

These builders are the only place where output dicts are assembled, so
field sets and insertion order live in exactly one spot.  The voucher
builder also owns the final business rule of the pipeline: debit and
credit totals must balance before a result may exist.
"""
from __future__ import annotations

from ._amounts import format_cents
from ._errors import UnbalancedVoucherError


def build_entry_result(
    *,
    line_no: int,
    account: dict,
    summary: str,
    debit_cents: int,
    credit_cents: int,
) -> dict:
    """Assemble one normalized entry line (field order is contractual)."""
    return {
        "line_no": line_no,
        "account_code": account["code"],
        "account_name": account["name"],
        "normal_side": account["normal_side"],
        "summary": summary,
        "debit": format_cents(debit_cents),
        "credit": format_cents(credit_cents),
    }


def build_voucher_result(
    *,
    voucher_id: str,
    date_text: str,
    currency: str,
    entry_results: list[dict],
    debit_total_cents: int,
    credit_total_cents: int,
    where: str,
) -> dict:
    """Check the balance rule and assemble one normalized voucher."""
    if debit_total_cents != credit_total_cents:
        raise UnbalancedVoucherError(
            f"{where}: debit total "
            f"{format_cents(debit_total_cents)} != credit total "
            f"{format_cents(credit_total_cents)}"
        )
    return {
        "voucher_id": voucher_id,
        "date": date_text,
        "currency": currency,
        "entries": entry_results,
        "debit_total": format_cents(debit_total_cents),
        "credit_total": format_cents(credit_total_cents),
    }
