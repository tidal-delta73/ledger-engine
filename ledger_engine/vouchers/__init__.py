"""Validation and normalization of raw double-entry vouchers.

Public surface (re-exported unchanged) plus the independently testable
rule stages the pipeline composes:

* :mod:`ledger_engine.vouchers.errors`    -- exception hierarchy;
* :mod:`ledger_engine.vouchers.amounts`   -- integer-cents arithmetic;
* :mod:`ledger_engine.vouchers.chart`     -- chart parsing/snapshot;
* :mod:`ledger_engine.vouchers.structure` -- voucher shape checks;
* :mod:`ledger_engine.vouchers.entries`   -- entry business rules;
* :mod:`ledger_engine.vouchers.assemble`  -- result construction;
* :mod:`ledger_engine.vouchers.pipeline`  -- phase orchestration;
* :mod:`ledger_engine.vouchers.posting`   -- journal/balance assembly.
* :mod:`ledger_engine.vouchers.trial_balance` -- trial balance assembly.

Pure-Python, no runtime dependencies, no global state.  Inputs are never
mutated; on success new JSON-serializable containers are returned in the
same order.  Validation stops at the first problem encountered.
"""
from __future__ import annotations

from .errors import (
    ChartOfAccountsError,
    DuplicateVoucherError,
    InactiveAccountError,
    InvalidEntryAmountError,
    LedgerEngineError,
    UnsupportedCurrencyError,
    UnbalancedVoucherError,
    UnknownAccountError,
    VoucherFormatError,
)
from .pipeline import normalize_vouchers
from .posting import post_vouchers
from .trial_balance import build_trial_balance

__all__ = [
    "normalize_vouchers",
    "post_vouchers",
    "build_trial_balance",
    "LedgerEngineError",
    "ChartOfAccountsError",
    "VoucherFormatError",
    "DuplicateVoucherError",
    "UnsupportedCurrencyError",
    "UnbalancedVoucherError",
    "UnknownAccountError",
    "InactiveAccountError",
    "InvalidEntryAmountError",
]
