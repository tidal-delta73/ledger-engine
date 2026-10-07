# ledger-engine

Double-entry ledger and reconciliation engine.

Pure-Python, no runtime dependencies.

## Usage

```bash
python3 -m ledger_engine version
python3 -m ledger_engine help
```

## Reconciliation

`build_reconciliation_report(request)` builds a reproducible book-vs-
statement difference report at one cutoff. It only reads ledger facts
already effective by the cutoff and not cancelled by an in-scope
reversal; it never posts, reverses, closes or rewrites a historical
amount at a current rate.

The request names the book set, account, cutoff, posted book facts and
the external snapshot (each external detail carries a source-unique id,
business date, currency, signed amount and an optional voucher
reference). Matching first confirms the same business through the
source id / voucher reference relation, then compares currency,
original-currency amount and the saved base-currency amount; only
records without a business identity may fall back to a one-to-one
attribute match on (account, currency, amount, date). Non-unique
candidates are reported as ambiguous, never picked by input order.

Every call returns a serializable outcome envelope:

- `reconciled` — seven difference categories (`matched`,
  `ledger_missing`, `external_missing`, `amount_mismatch`,
  `currency_mismatch`, `base_amount_mismatch`, `ambiguous`) plus a
  per-currency summary and one base-currency difference (original
  amounts of different currencies are never added together);
- `invalid_input` — including a duplicate external source id (no partial
  report); an empty snapshot is valid and lists in-scope facts as
  `external_missing`;
- `target_not_found` — unknown book set or account;
- `period_status_conflict` — the cutoff falls in a recorded historical
  period whose close is unfinished.

Reports are byte-identical for the same facts and semantically equal
details regardless of input order, hash seed, locale or current time.
