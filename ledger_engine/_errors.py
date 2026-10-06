"""Exception hierarchy shared by every ledger_engine validation stage.

Each leaf class names exactly one failure category, so callers can catch
the precise problem without parsing messages.  All leaves derive from
:class:`LedgerEngineError` and from nothing else.
"""


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
