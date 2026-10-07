"""Book-vs-statement reconciliation: reproducible difference reports.

This subpackage adds one read-only capability on top of the posted
ledger: locating differences between the ledger facts already effective
by a cutoff and an external statement snapshot.  It never posts, reverses
or closes anything, never mutates its inputs, and never rewrites a
historical amount with a rate fetched at call time.

Public surface:

* :func:`build_reconciliation_report` -- the single total entry point,
  always returning a serializable outcome envelope;
* the ``OUTCOME_*`` result codes and ``DIFF_*`` difference categories;
* the ``MATCH_*`` match-method codes.

The stage layout mirrors ``vouchers``:

* :mod:`ledger_engine.reconciliation.contract` -- stable string codes;
* :mod:`ledger_engine.reconciliation.amounts`  -- signed integer cents;
* :mod:`ledger_engine.reconciliation.periods`  -- period calendar;
* :mod:`ledger_engine.reconciliation.models`   -- immutable records;
* :mod:`ledger_engine.reconciliation.engine`   -- scoping and matching;
* :mod:`ledger_engine.reconciliation.assembly` -- deterministic assembly;
* :mod:`ledger_engine.reconciliation.report`   -- boundary orchestration.
"""
from __future__ import annotations

from .contract import (
    DIFF_AMBIGUOUS,
    DIFF_AMOUNT_MISMATCH,
    DIFF_BASE_AMOUNT_MISMATCH,
    DIFF_CURRENCY_MISMATCH,
    DIFF_EXTERNAL_MISSING,
    DIFF_LEDGER_MISSING,
    DIFF_MATCHED,
    MATCH_ATTRIBUTES,
    MATCH_SOURCE_ID,
    MATCH_VOUCHER_REF,
    OUTCOME_INVALID_INPUT,
    OUTCOME_PERIOD_STATUS_CONFLICT,
    OUTCOME_RECONCILED,
    OUTCOME_TARGET_NOT_FOUND,
)
from .report import build_reconciliation_report

__all__ = [
    "build_reconciliation_report",
    "OUTCOME_RECONCILED",
    "OUTCOME_TARGET_NOT_FOUND",
    "OUTCOME_PERIOD_STATUS_CONFLICT",
    "OUTCOME_INVALID_INPUT",
    "DIFF_MATCHED",
    "DIFF_LEDGER_MISSING",
    "DIFF_EXTERNAL_MISSING",
    "DIFF_AMOUNT_MISMATCH",
    "DIFF_CURRENCY_MISMATCH",
    "DIFF_BASE_AMOUNT_MISMATCH",
    "DIFF_AMBIGUOUS",
    "MATCH_SOURCE_ID",
    "MATCH_VOUCHER_REF",
    "MATCH_ATTRIBUTES",
]
