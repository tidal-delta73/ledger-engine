"""Exception categories shared by every stage of voucher processing.

The hierarchy is part of the public contract (``ledger_engine`` re-exports
these names) and must stay stable: each leaf class denotes one observable
failure boundary, and the validation pipeline reports the *first* boundary
hit -- never the base class.
"""
from __future__ import annotations

__all__ = [
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
