"""Validation and normalization of raw double-entry vouchers.

Pure-Python, no runtime dependencies, no global state. Inputs are never
mutated; on success a new list of JSON-serializable dicts is returned in
the same order. Validation stops at the first problem encountered.

This module is the public facade and pipeline orchestrator.  Each stage
of the pipeline lives in its own module so the deterministic rules can
be reused (posting, reversal, recalculation) without copying logic:

- ``_chart``:     base-currency check and chart-of-accounts parsing
- ``_structure``: structural (shape/type) checks for vouchers and entries
- ``_entries``:   per-entry business rules (account, one-sided amounts)
- ``_amounts``:   integer-cent fixed-point arithmetic
- ``_results``:   normalized result construction and the balance rule
- ``_errors``:    the shared exception hierarchy
"""
from __future__ import annotations

from ._chart import parse_chart, validate_base_currency
from ._entries import validate_entry_business
from ._errors import (
    ChartOfAccountsError,
    DuplicateVoucherError,
    InactiveAccountError,
    InvalidEntryAmountError,
    LedgerEngineError,
    UnbalancedVoucherError,
    UnknownAccountError,
    UnsupportedCurrencyError,
    VoucherFormatError,
)
from ._results import build_entry_result, build_voucher_result
from ._structure import validate_voucher_structure

__all__ = [
    "normalize_vouchers",
    "LedgerEngineError",
    "ChartOfAccountsError",
    "VoucherFormatError",
    "DuplicateVoucherError",
    "UnsupportedCurrencyError",
    "UnknownAccountError",
    "InactiveAccountError",
    "InvalidEntryAmountError",
    "UnbalancedVoucherError",
]


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
    validate_base_currency(base_currency)
    accounts = parse_chart(chart_of_accounts)
    if not isinstance(vouchers, list):
        raise VoucherFormatError("vouchers must be a list")

    seen_ids: set[str] = set()
    results: list[dict] = []

    for v_index, voucher in enumerate(vouchers):
        where = f"voucher #{v_index}"

        # --- Phase 1: structural/format checks (VoucherFormatError) ---
        voucher_id, date_text, currency, entries = validate_voucher_structure(
            voucher, where=where
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
        entry_results: list[dict] = []
        debit_total = 0
        credit_total = 0
        for e_index, entry in enumerate(entries):
            entry_where = f"{where} ({voucher_id}) line {e_index + 1}"
            account, debit_cents, credit_cents = validate_entry_business(
                entry, accounts, where=entry_where
            )
            debit_total += debit_cents
            credit_total += credit_cents
            entry_results.append(
                build_entry_result(
                    line_no=e_index + 1,
                    account=account,
                    summary=entry["summary"],
                    debit_cents=debit_cents,
                    credit_cents=credit_cents,
                )
            )

        # --- Phase 4: balance, then result construction ---
        results.append(
            build_voucher_result(
                voucher_id=voucher_id,
                date_text=date_text,
                currency=currency,
                entry_results=entry_results,
                debit_total_cents=debit_total,
                credit_total_cents=credit_total,
                where=f"{where} ({voucher_id})",
            )
        )

    return results
