"""Internal control-flow signals for reconciliation validation.

None of the reconciliation feature's *public* failure modes are raised:
the contract is that every call -- even with a malformed snapshot, a
missing book/account or an unfinished historical period -- *returns* a
serializable outcome document (``invalid_input``, ``target_not_found`` or
``period_status_conflict``).  That keeps report generation total and lets
callers persist or diff any outcome byte-for-byte.

Validation stages therefore raise this private signal purely to unwind to
the single boundary that converts it into the ``invalid_input`` envelope;
it is never exported and is not a :class:`LedgerEngineError`.
"""
from __future__ import annotations

__all__ = ["ValidationAborted"]


class ValidationAborted(Exception):
    """Raised internally to stop validation with one reason string."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
