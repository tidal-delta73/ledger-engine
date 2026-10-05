# ledger-engine

Double-entry ledger and reconciliation engine.

Pure-Python, no runtime dependencies.

## Usage

```bash
python3 -m ledger_engine version
python3 -m ledger_engine help
```

## Normalizing vouchers

```python
from ledger_engine import normalize_vouchers

result = normalize_vouchers(functional_currency, chart_of_accounts, vouchers)
```

Validates a batch of raw vouchers in input order and returns a new list of
normalized vouchers (inputs are never modified). Validation stops at the first
problem, raising one of the exceptions under `ledger_engine.errors`:

- `ChartOfAccountsError` — invalid functional currency (three uppercase
  letters), or an invalid/duplicate account in the chart
- `VoucherFormatError` — malformed voucher/entry fields, types, dates
  (`YYYY-MM-DD`), or fewer than two entries
- `DuplicateVoucherError` — repeated `voucher_id` within the batch
- `UnsupportedCurrencyError` — voucher currency differs from the functional
  currency
- `UnknownAccountError` / `InactiveAccountError` — entry account missing from
  or disabled in the chart (codes match exactly, never trimmed or padded)
- `InvalidEntryAmountError` — amount is not a fixed-point decimal string (no
  sign, exponent or thousands separator; at most two fraction digits), or
  debit/credit are both zero or both nonzero
- `UnbalancedVoucherError` — voucher debit and credit totals differ

Each normalized entry gets a 1-based `line_no`, the account `name` and
`normal_side` from the chart, and amounts formatted with exactly two decimal
places (`0.00` for zero). Each voucher includes `debit_total` and
`credit_total`. The result contains only JSON-serializable basic types.

