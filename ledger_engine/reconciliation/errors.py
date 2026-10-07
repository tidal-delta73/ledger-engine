"""Exception categories for book-to-external reconciliation.

The three leaf classes denote the three *fixed* (deterministic,
classification-bearing) input boundaries the reconciliation entry point
can reject a call with.  They extend
:class:`ledger_engine.vouchers.errors.LedgerEngineError` so callers that
already catch the engine base class keep working; the reconciliation
leaf classes themselves are part of the public contract and are
re-exported from the top-level package.
"""
from __future__ import annotations

from ..vouchers.errors import LedgerEngineError

__all__ = [
    "InvalidReconciliationInputError",
    "TargetNotFoundError",
    "PeriodStatusConflictError",
]


class InvalidReconciliationInputError(LedgerEngineError):
    """The external snapshot is invalid (e.g. a duplicate source id).

    The call result is fixed to "invalid input": no partial report is
    ever produced.
    """


class TargetNotFoundError(LedgerEngineError):
    """The requested book set or account does not exist in the catalog."""


class PeriodStatusConflictError(LedgerEngineError):
    """The cutoff falls in a historical period that is not closed."""
