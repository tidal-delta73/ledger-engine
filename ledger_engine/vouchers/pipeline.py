"""Pipeline orchestration for voucher normalization.

The public :func:`normalize_vouchers` only sequences the independently
testable stages; it contains no validation or arithmetic logic of its
own.  The observable order is contractual:

1. base currency, then chart of accounts;
2. the batch must be a list;
3. per voucher, in batch order:
   a. *all* structural checks for the voucher and every entry,
   b. batch duplicate voucher_id,
   c. currency equals the base currency,
   d. per entry, in line order: unknown account, inactive account,
      debit/credit amount syntax, exactly-one-sidedness,
   e. debit/credit balance,
   f. result construction.

Only the first problem reached is reported, and inputs are never
mutated; repeated calls return equal, brand-new containers.
"""
from __future__ import annotations

from .amounts import format_cents
from .assemble import build_voucher
from .chart import parse_chart
from .entries import validate_entries
from .errors import (
    DuplicateVoucherError,
    UnsupportedCurrencyError,
    UnbalancedVoucherError,
)
from .structure import require_voucher_list, structure_voucher

__all__ = ["normalize_vouchers"]


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
    # Stage 0: base currency + chart of accounts -> immutable snapshot.
    chart = parse_chart(base_currency, chart_of_accounts)
    # Stage 0b: batch container shape.
    batch = require_voucher_list(vouchers)

    seen_ids: set[str] = set()
    results: list[dict] = []

    for v_index, voucher in enumerate(batch):
        where = f"voucher #{v_index}"

        # Stage 1: every structural check (voucher and all its entries).
        structured = structure_voucher(voucher, v_index)
        voucher_id = structured.voucher_id

        # Stage 2a: batch uniqueness (exact match).
        if voucher_id in seen_ids:
            raise DuplicateVoucherError(
                f"{where}: duplicate voucher_id {voucher_id!r}"
            )
        seen_ids.add(voucher_id)

        # Stage 2b: single bookkeeping currency.
        if structured.currency != chart.base_currency:
            raise UnsupportedCurrencyError(
                f"{where} ({voucher_id}): currency {structured.currency!r} "
                f"does not match base currency {chart.base_currency!r}"
            )

        # Stage 3: entry business rules and integer-cents totals.
        body = validate_entries(structured, chart, v_index)

        # Stage 4: balance is checked last.
        if body.debit_total != body.credit_total:
            raise UnbalancedVoucherError(
                f"{where} ({voucher_id}): debit total "
                f"{format_cents(body.debit_total)} != credit total "
                f"{format_cents(body.credit_total)}"
            )

        # Stage 5: deterministic result assembly.
        results.append(build_voucher(structured, body))

    return results
