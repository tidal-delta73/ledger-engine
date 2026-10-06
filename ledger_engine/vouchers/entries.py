"""Stage 2: per-entry business validation against the parsed chart.

This is the single place entry-level business rules live: exact account
resolution, the active flag, integer-cents amount parsing, the
exactly-one-side rule and running debit/credit totals.  Posting,
reversals and recomputation reuse these rules instead of copying them.

Checks run strictly in entry order and the first boundary hit wins:
unknown account, then inactive account, then debit amount syntax, then
credit amount syntax, then exactly-one-sidedness.
"""
from __future__ import annotations

from dataclasses import dataclass

from .amounts import parse_cents
from .chart import Chart
from .errors import (
    InactiveAccountError,
    InvalidEntryAmountError,
    UnknownAccountError,
)
from .structure import StructuredVoucher

__all__ = ["ValidatedEntry", "validate_entries"]


@dataclass(frozen=True)
class ValidatedEntry:
    """One fully validated entry, amounts held as integer cents."""

    line_no: int
    account_code: str
    account_name: str
    normal_side: str
    summary: str
    debit_cents: int
    credit_cents: int


@dataclass(frozen=True)
class ValidatedVoucherBody:
    """Validated entries plus their exact integer-cents totals."""

    entries: tuple[ValidatedEntry, ...]
    debit_total: int
    credit_total: int


def validate_entries(
    structured: StructuredVoucher, chart: Chart, v_index: int
) -> ValidatedVoucherBody:
    """Resolve and validate every entry in order, accumulating totals."""
    where = f"voucher #{v_index}"
    voucher_id = structured.voucher_id
    validated: list[ValidatedEntry] = []
    debit_total = 0
    credit_total = 0

    for e_index, entry in enumerate(structured.entries):
        entry_where = f"{where} ({voucher_id}) line {e_index + 1}"
        account = chart.get(entry.account_code)  # exact match, no normalization
        if account is None:
            raise UnknownAccountError(
                f"{entry_where}: unknown account code "
                f"{entry.account_code!r}"
            )
        if not account.active:
            raise InactiveAccountError(
                f"{entry_where}: account {entry.account_code!r} is inactive"
            )

        debit_cents = parse_cents(entry.debit_text, where=entry_where)
        credit_cents = parse_cents(entry.credit_text, where=entry_where)
        if (debit_cents == 0) == (credit_cents == 0):
            raise InvalidEntryAmountError(
                f"{entry_where}: exactly one of debit/credit must be non-zero"
            )

        debit_total += debit_cents
        credit_total += credit_cents
        validated.append(
            ValidatedEntry(
                line_no=e_index + 1,
                account_code=account.code,
                account_name=account.name,
                normal_side=account.normal_side,
                summary=entry.summary,
                debit_cents=debit_cents,
                credit_cents=credit_cents,
            )
        )

    return ValidatedVoucherBody(
        entries=tuple(validated),
        debit_total=debit_total,
        credit_total=credit_total,
    )
