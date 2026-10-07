"""Stable string constants for the reconciliation feature.

The reconciliation report deliberately uses *string* codes rather than
Python objects for categories, match methods and outcome states, so a
serialized report stays self-describing and byte-comparable across
processes.  These names are part of the public contract and must never be
renamed silently: tests pin the literals.
"""
from __future__ import annotations

__all__ = [
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
    "DIFF_CATEGORIES",
    "MATCH_SOURCE_ID",
    "MATCH_VOUCHER_REF",
    "MATCH_ATTRIBUTES",
]

#: A report was produced.
OUTCOME_RECONCILED = "reconciled"
#: The book set or the account does not exist.
OUTCOME_TARGET_NOT_FOUND = "target_not_found"
#: The cutoff lies inside a historical period whose close is unfinished.
OUTCOME_PERIOD_STATUS_CONFLICT = "period_status_conflict"
#: The external snapshot (or request) is structurally invalid.
OUTCOME_INVALID_INPUT = "invalid_input"

#: Every compared attribute agrees.
DIFF_MATCHED = "matched"
#: An external detail has no effective un-offset ledger fact.
DIFF_LEDGER_MISSING = "ledger_missing"
#: An in-scope ledger fact has no external counterpart.
DIFF_EXTERNAL_MISSING = "external_missing"
#: Currencies agree but the original-currency amounts do not.
DIFF_AMOUNT_MISMATCH = "amount_mismatch"
#: The two records name different currencies.
DIFF_CURRENCY_MISMATCH = "currency_mismatch"
#: Original currency and amount agree but the saved base-currency
#: amount differs (or is recorded on exactly one side).
DIFF_BASE_AMOUNT_MISMATCH = "base_amount_mismatch"
#: A one-to-one matching decision is not uniquely determined.
DIFF_AMBIGUOUS = "ambiguous"

#: Stable category ordering for serialization (independent of dict order).
DIFF_CATEGORIES = (
    DIFF_MATCHED,
    DIFF_LEDGER_MISSING,
    DIFF_EXTERNAL_MISSING,
    DIFF_AMOUNT_MISMATCH,
    DIFF_CURRENCY_MISMATCH,
    DIFF_BASE_AMOUNT_MISMATCH,
    DIFF_AMBIGUOUS,
)

#: Matched via the source-unique identifier.
MATCH_SOURCE_ID = "source_id"
#: Matched via the external voucher reference carried on the ledger fact.
MATCH_VOUCHER_REF = "voucher_ref"
#: One-to-one fallback on (account, currency, signed amount, date).
MATCH_ATTRIBUTES = "attributes"
