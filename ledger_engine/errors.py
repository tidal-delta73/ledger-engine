"""Validation errors raised by :func:`ledger_engine.normalize_vouchers`."""


class LedgerEngineError(Exception):
    """Base class for all ledger_engine validation errors."""


class ChartOfAccountsError(LedgerEngineError):
    """Functional currency is invalid, or the chart of accounts is malformed."""


class VoucherFormatError(LedgerEngineError):
    """A voucher or one of its entries has invalid fields or field types."""


class DuplicateVoucherError(LedgerEngineError):
    """Two vouchers in the same batch share a voucher_id."""


class UnsupportedCurrencyError(LedgerEngineError):
    """A voucher currency does not equal the functional currency."""


class UnknownAccountError(LedgerEngineError):
    """An entry references an account_code absent from the chart of accounts."""


class InactiveAccountError(LedgerEngineError):
    """An entry references an account marked inactive."""


class InvalidEntryAmountError(LedgerEngineError):
    """An entry amount is malformed, or debit/credit are not mutually exclusive."""


class UnbalancedVoucherError(LedgerEngineError):
    """A voucher's debit total does not equal its credit total."""
