# ledger-engine

Double-entry ledger and reconciliation engine.

Pure-Python, no runtime dependencies.

## Usage

```bash
python3 -m ledger_engine version
python3 -m ledger_engine help
```

## Modules

- `ledger_engine.vouchers` — voucher normalization, posting, batch
  reversal and trial balance (single base currency, integer-cents
  arithmetic, deterministic serialization).
- `ledger_engine.reconciliation` — book-to-external difference
  localization: `build_reconciliation_report(book_sets, book_set_code,
  account_code, cutoff, external_details)` builds a reproducible
  reconciliation report as of one cutoff, comparing posted, non-reversed
  ledger facts against an external snapshot.  It is strictly read-only
  and never changes posting, reversal, closing, balance or existing
  report behavior, and never rewrites historical amounts at the latest
  exchange rate.

### Reconciliation report

- Identity is confirmed first by the external source's unique id echoed
  on the books or by the voucher reference; only records with no
  business identifier fall back to one-to-one matching on
  `(account, currency, original amount, business date)`.  Non-unique
  candidates are always reported as `ambiguous`, never picked by input
  order.
- Difference categories: `exact_match`, `missing_on_books`,
  `missing_externally`, `original_amount_mismatch`, `currency_mismatch`,
  `base_conversion_mismatch`, `ambiguous`.
- Each item carries both sides' record ids, compared amounts, recorded
  rate evidence and traceable voucher references.
- Summaries are per currency (original-currency amounts are never summed
  across currencies) plus base-currency totals and gaps.
- Fixed boundaries: duplicate external source ids (and malformed
  payloads) raise `InvalidReconciliationInputError` with no partial
  report; unknown book set/account raises `TargetNotFoundError`; a cutoff
  in an unclosed historical period raises `PeriodStatusConflictError`.
  An empty snapshot is valid (all in-scope facts become
  `missing_externally`).
- Reports are byte-identical regardless of input order, hash seed,
  locale or current time, and closed-period historical reports stay
  stable after later vouchers, rates or reversals.
