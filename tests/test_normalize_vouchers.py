"""Regression tests for the public entry point ``normalize_vouchers``.

Pure stdlib (unittest). No dependence on the current working directory,
the local timezone, dict/set iteration order, or third-party packages.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import unittest

# Make the repository importable regardless of the current working directory.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ledger_engine import (  # noqa: E402
    ChartOfAccountsError,
    DuplicateVoucherError,
    InactiveAccountError,
    InvalidEntryAmountError,
    LedgerEngineError,
    UnbalancedVoucherError,
    UnknownAccountError,
    UnsupportedCurrencyError,
    VoucherFormatError,
    normalize_vouchers,
)

BASE = "USD"

# Fixed JSON serialization parameters used for byte-level comparisons.
JSON_KWARGS = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}


def make_chart():
    """A fresh chart: two debit-nature, one credit-nature, one inactive."""
    return [
        {"code": "1001", "name": "Cash on hand",
         "normal_side": "debit", "active": True},
        {"code": "1002", "name": "Bank deposit",
         "normal_side": "debit", "active": True},
        {"code": "4001", "name": "Operating revenue",
         "normal_side": "credit", "active": True},
        {"code": "5001", "name": "Legacy account",
         "normal_side": "debit", "active": False},
    ]


def make_entry(account_code, debit="0.00", credit="0.00", summary="entry"):
    return {
        "account_code": account_code,
        "summary": summary,
        "debit": debit,
        "credit": credit,
    }


def make_voucher(voucher_id="V-2026-001", date="2026-01-15", currency=BASE,
                 entries=None):
    if entries is None:
        entries = [
            make_entry("1001", debit="10"),
            make_entry("4001", credit="10.00"),
        ]
    return {
        "voucher_id": voucher_id,
        "date": date,
        "currency": currency,
        "entries": entries,
    }


def normalize(vouchers, chart=None, base=BASE):
    return normalize_vouchers(base, make_chart() if chart is None else chart,
                              vouchers)


class ValidNormalizationTests(unittest.TestCase):
    """Happy-path contract: shape, order, formatting, chart enrichment."""

    def test_multiple_vouchers_multiple_accounts_and_amount_shapes(self):
        vouchers = [
            make_voucher("V-1", entries=[
                make_entry("1001", debit="10"),          # integer string
                make_entry("1002", debit="0.05"),        # two decimals
                make_entry("4001", credit="10.05"),
            ]),
            make_voucher("V-2", entries=[
                make_entry("1002", debit="7.5"),         # one decimal
                make_entry("4001", credit="7.50"),
            ]),
            make_voucher("V-3", entries=[
                make_entry("1001", debit="0.01"),
                make_entry("4001", credit="0.01"),
            ]),
        ]
        result = normalize(vouchers)

        # Voucher order is preserved.
        self.assertEqual([v["voucher_id"] for v in result],
                         ["V-1", "V-2", "V-3"])
        # Entry order is preserved and line numbers are 1-based.
        self.assertEqual([e["line_no"] for e in result[0]["entries"]],
                         [1, 2, 3])
        self.assertEqual([e["account_code"] for e in result[0]["entries"]],
                         ["1001", "1002", "4001"])

        # Amounts and totals are normalized to exactly two decimals.
        first = result[0]["entries"]
        self.assertEqual((first[0]["debit"], first[0]["credit"]),
                         ("10.00", "0.00"))
        self.assertEqual((first[1]["debit"], first[1]["credit"]),
                         ("0.05", "0.00"))
        self.assertEqual((first[2]["debit"], first[2]["credit"]),
                         ("0.00", "10.05"))
        self.assertEqual(result[0]["debit_total"], "10.05")
        self.assertEqual(result[0]["credit_total"], "10.05")
        self.assertEqual(result[1]["entries"][0]["debit"], "7.50")
        self.assertEqual(result[1]["debit_total"], "7.50")

        # Account name and normal side come from the chart, not the input.
        self.assertEqual(first[0]["account_name"], "Cash on hand")
        self.assertEqual(first[0]["normal_side"], "debit")
        self.assertEqual(first[2]["account_name"], "Operating revenue")
        self.assertEqual(first[2]["normal_side"], "credit")

        # Voucher-level fields are carried through verbatim.
        self.assertEqual(result[0]["date"], "2026-01-15")
        self.assertEqual(result[0]["currency"], BASE)

    def test_result_is_json_serializable(self):
        result = normalize([make_voucher()])
        text = json.dumps(result, **JSON_KWARGS)
        self.assertIsInstance(text, str)
        self.assertEqual(json.loads(text), result)

    def test_empty_batch_is_valid(self):
        self.assertEqual(normalize([]), [])

    def test_leap_day_boundary(self):
        ok = make_voucher("V-LEAP", date="2024-02-29")  # 2024 is a leap year
        result = normalize([ok])
        self.assertEqual(result[0]["date"], "2024-02-29")

    def test_zero_amount_variants_normalize_to_zero(self):
        for zero in ("0", "00", "0.0", "0.00"):
            voucher = make_voucher(entries=[
                make_entry("1001", debit="3.25", credit=zero),
                make_entry("4001", debit=zero, credit="3.25"),
            ])
            result = normalize([voucher])
            self.assertEqual(result[0]["entries"][0]["credit"], "0.00")
            self.assertEqual(result[0]["entries"][1]["debit"], "0.00")
            self.assertEqual(result[0]["debit_total"], "3.25")

    def test_huge_amounts_keep_exact_precision(self):
        big = "123456789012345678901234567890.55"
        voucher = make_voucher(entries=[
            make_entry("1001", debit=big),
            make_entry("4001", credit=big),
        ])
        result = normalize([voucher])
        self.assertEqual(result[0]["entries"][0]["debit"], big)
        self.assertEqual(result[0]["debit_total"], big)
        self.assertEqual(result[0]["credit_total"], big)

    def test_huge_mixed_totals_sum_without_float_error(self):
        voucher = make_voucher(entries=[
            make_entry("1001", debit="999999999999999999.99"),
            make_entry("1002", debit="0.01"),
            make_entry("4001", credit="1000000000000000000.00"),
        ])
        result = normalize([voucher])
        self.assertEqual(result[0]["debit_total"], "1000000000000000000.00")
        self.assertEqual(result[0]["credit_total"], "1000000000000000000.00")


class DeterminismTests(unittest.TestCase):
    """Repeated calls on the same input: equal objects, identical bytes."""

    def test_repeated_calls_return_equal_but_distinct_objects(self):
        vouchers = [make_voucher("V-1"), make_voucher("V-2")]
        first = normalize(vouchers)
        second = normalize(vouchers)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first[0], second[0])
        self.assertIsNot(first[0]["entries"], second[0]["entries"])

    def test_fixed_json_parameters_give_byte_identical_text(self):
        vouchers = [make_voucher("V-1"), make_voucher("V-2")]
        text1 = json.dumps(normalize(vouchers), **JSON_KWARGS)
        text2 = json.dumps(normalize(vouchers), **JSON_KWARGS)
        self.assertEqual(text1, text2)
        self.assertEqual(text1.encode("utf-8"), text2.encode("utf-8"))


class ImmutabilityTests(unittest.TestCase):
    """Inputs are never mutated, on success or on failure."""

    def assert_inputs_untouched(self, base, chart, vouchers):
        snapshot = (copy.deepcopy(base), copy.deepcopy(chart),
                    copy.deepcopy(vouchers))
        try:
            normalize_vouchers(base, chart, vouchers)
        except LedgerEngineError:
            pass
        self.assertEqual(base, snapshot[0])
        self.assertEqual(chart, snapshot[1])
        self.assertEqual(vouchers, snapshot[2])

    def test_success_does_not_mutate_inputs(self):
        self.assert_inputs_untouched(
            BASE, make_chart(), [make_voucher("V-1"), make_voucher("V-2")])

    def test_failure_does_not_mutate_inputs(self):
        bad = make_voucher(entries=[
            make_entry("1001", debit="1.00"),
            make_entry("9999", credit="1.00"),  # unknown account
        ])
        self.assert_inputs_untouched(BASE, make_chart(), [bad])

    def test_failure_mid_batch_does_not_mutate_inputs(self):
        vouchers = [
            make_voucher("V-1"),
            make_voucher("V-2", entries=[
                make_entry("1001", debit="1.00"),
                make_entry("4001", credit="2.00"),  # unbalanced
            ]),
        ]
        self.assert_inputs_untouched(BASE, make_chart(), vouchers)


class ExceptionHierarchyTests(unittest.TestCase):
    def test_all_public_errors_derive_from_base(self):
        for cls in (ChartOfAccountsError, VoucherFormatError,
                    DuplicateVoucherError, UnsupportedCurrencyError,
                    UnknownAccountError, InactiveAccountError,
                    InvalidEntryAmountError, UnbalancedVoucherError):
            self.assertTrue(issubclass(cls, LedgerEngineError), cls)


class ChartOfAccountsErrorTests(unittest.TestCase):
    """Invalid base currency or invalid chart -> ChartOfAccountsError."""

    def assert_chart_error(self, base, chart):
        with self.assertRaises(ChartOfAccountsError):
            normalize_vouchers(base, chart, [make_voucher()])

    def test_valid_base_currency_boundary(self):
        # Exactly three uppercase letters is accepted.
        voucher = make_voucher(currency="EUR")
        self.assertEqual(normalize([voucher], base="EUR")[0]["currency"],
                         "EUR")

    def test_invalid_base_currencies(self):
        for bad in ("US", "USDD", "usd", "Usd", "US1", "U D", "", 123, None,
                    ["USD"]):
            with self.subTest(bad=bad):
                self.assert_chart_error(bad, make_chart())

    def test_chart_must_be_a_list(self):
        for bad in ({"1001": {}}, "chart", None, 42):
            with self.subTest(bad=bad):
                self.assert_chart_error(BASE, bad)

    def test_chart_item_must_be_an_object(self):
        self.assert_chart_error(BASE, ["1001"])
        self.assert_chart_error(BASE, [None])

    def test_chart_missing_fields(self):
        full = {"code": "1001", "name": "Cash", "normal_side": "debit",
                "active": True}
        for missing in ("code", "name", "normal_side", "active"):
            item = {k: v for k, v in full.items() if k != missing}
            with self.subTest(missing=missing):
                self.assert_chart_error(BASE, [item])

    def test_chart_field_type_errors(self):
        base_item = {"code": "1001", "name": "Cash",
                     "normal_side": "debit", "active": True}
        bad_items = [
            dict(base_item, code=""),            # empty code
            dict(base_item, code=1001),          # non-string code
            dict(base_item, name=""),            # empty name
            dict(base_item, name=None),          # non-string name
            dict(base_item, normal_side="Debit"),   # case-sensitive
            dict(base_item, normal_side="both"),
            dict(base_item, active=1),           # int is not bool
            dict(base_item, active="yes"),
        ]
        for item in bad_items:
            with self.subTest(item=item):
                self.assert_chart_error(BASE, [item])

    def test_duplicate_account_code_exact_match(self):
        chart = make_chart()
        chart.append({"code": "1001", "name": "Duplicate",
                      "normal_side": "credit", "active": True})
        self.assert_chart_error(BASE, chart)

    def test_similar_but_distinct_codes_are_allowed(self):
        chart = make_chart()
        chart.append({"code": "1001 ", "name": "Trailing space",
                      "normal_side": "debit", "active": True})
        chart.append({"code": "１００１", "name": "Fullwidth digits",
                      "normal_side": "debit", "active": True})
        result = normalize([make_voucher()], chart=chart)
        self.assertEqual(len(result), 1)

    def test_empty_chart_is_valid_but_accounts_become_unknown(self):
        with self.assertRaises(UnknownAccountError):
            normalize([make_voucher()], chart=[])

    def test_chart_checked_before_vouchers(self):
        # Both chart and vouchers are broken: the chart error wins.
        with self.assertRaises(ChartOfAccountsError):
            normalize_vouchers(BASE, "not-a-list", "also-not-a-list")

    def test_base_currency_checked_before_chart(self):
        with self.assertRaises(ChartOfAccountsError) as ctx:
            normalize_vouchers("usd", "not-a-list", [])
        self.assertIn("base currency", str(ctx.exception))


class VoucherFormatErrorTests(unittest.TestCase):
    """Structural/type problems in vouchers -> VoucherFormatError."""

    def assert_format_error(self, vouchers):
        with self.assertRaises(VoucherFormatError):
            normalize(vouchers)

    def test_vouchers_must_be_a_list(self):
        for bad in ("vouchers", {"v": 1}, None, 42):
            with self.subTest(bad=bad):
                self.assert_format_error(bad)

    def test_voucher_must_be_an_object(self):
        self.assert_format_error(["not-a-dict"])
        self.assert_format_error([None])

    def test_voucher_missing_fields(self):
        full = make_voucher()
        for missing in ("voucher_id", "date", "currency", "entries"):
            voucher = {k: v for k, v in full.items() if k != missing}
            with self.subTest(missing=missing):
                self.assert_format_error([voucher])

    def test_voucher_id_must_be_non_empty_string(self):
        for bad in ("", 123, None, ["V-1"], True):
            with self.subTest(bad=bad):
                self.assert_format_error([make_voucher(voucher_id=bad)])

    def test_date_must_be_valid_calendar_date(self):
        for bad in ("2026-02-29",   # 2026 is not a leap year
                    "2026-02-30",
                    "2026-13-01",
                    "2026-00-10",
                    "2026-01-32",
                    "2026-1-15",    # not zero-padded
                    "2026/01/15",
                    "2026-01-15 ",
                    " 2026-01-15",
                    "20260115",
                    20260115,
                    None):
            with self.subTest(bad=bad):
                self.assert_format_error([make_voucher(date=bad)])

    def test_currency_must_be_a_string(self):
        for bad in (123, None, ["USD"], True):
            with self.subTest(bad=bad):
                self.assert_format_error([make_voucher(currency=bad)])

    def test_entries_must_be_a_list_with_at_least_two_entries(self):
        single = [make_entry("1001", debit="1.00")]
        for bad in ("entries", {"a": 1}, None, [], single):
            voucher = make_voucher()
            voucher["entries"] = bad
            with self.subTest(bad=bad):
                self.assert_format_error([voucher])

    def test_entry_must_be_an_object(self):
        self.assert_format_error([make_voucher(entries=[
            make_entry("1001", debit="1.00"), "oops"])])

    def test_entry_missing_fields(self):
        full = make_entry("1001", debit="1.00")
        for missing in ("account_code", "summary", "debit", "credit"):
            entry = {k: v for k, v in full.items() if k != missing}
            with self.subTest(missing=missing):
                self.assert_format_error([make_voucher(entries=[
                    entry, make_entry("4001", credit="1.00")])])

    def test_entry_identifiers_must_be_strings(self):
        for field, bad in (("account_code", 1001), ("account_code", None),
                           ("summary", 42), ("summary", None),
                           ("summary", ["text"])):
            entry = make_entry("1001", debit="1.00")
            entry[field] = bad
            with self.subTest(field=field, bad=bad):
                self.assert_format_error([make_voucher(entries=[
                    entry, make_entry("4001", credit="1.00")])])


class DuplicateVoucherErrorTests(unittest.TestCase):
    def test_identical_ids_in_same_batch(self):
        with self.assertRaises(DuplicateVoucherError):
            normalize([make_voucher("V-1"), make_voucher("V-1")])

    def test_duplicate_detected_exactly_not_normalized(self):
        # Case, whitespace and padding differences make ids distinct.
        vouchers = [make_voucher("V-1"), make_voucher("v-1"),
                    make_voucher("V-1 "), make_voucher(" V-1"),
                    make_voucher("V-01")]
        result = normalize(vouchers)
        self.assertEqual([v["voucher_id"] for v in result],
                         ["V-1", "v-1", "V-1 ", " V-1", "V-01"])

    def test_numeric_lookalike_id_is_a_format_error_not_duplicate(self):
        with self.assertRaises(VoucherFormatError):
            normalize([make_voucher("1"), make_voucher(1)])


class UnsupportedCurrencyErrorTests(unittest.TestCase):
    def test_foreign_currency_rejected(self):
        with self.assertRaises(UnsupportedCurrencyError):
            normalize([make_voucher(currency="EUR")])

    def test_lowercase_currency_rejected_exact_match(self):
        with self.assertRaises(UnsupportedCurrencyError):
            normalize([make_voucher(currency="usd")])

    def test_padded_currency_rejected_exact_match(self):
        with self.assertRaises(UnsupportedCurrencyError):
            normalize([make_voucher(currency="USD ")])


class AccountLookupErrorTests(unittest.TestCase):
    def test_unknown_account(self):
        voucher = make_voucher(entries=[
            make_entry("1001", debit="1.00"),
            make_entry("9999", credit="1.00"),
        ])
        with self.assertRaises(UnknownAccountError):
            normalize([voucher])

    def test_account_code_compared_exactly(self):
        for code in (" 1001", "1001 ", "Cash", "1001\n"):
            voucher = make_voucher(entries=[
                make_entry(code, debit="1.00"),
                make_entry("4001", credit="1.00"),
            ])
            with self.subTest(code=code):
                with self.assertRaises(UnknownAccountError):
                    normalize([voucher])

    def test_inactive_account(self):
        voucher = make_voucher(entries=[
            make_entry("1001", debit="1.00"),
            make_entry("5001", credit="1.00"),  # present but inactive
        ])
        with self.assertRaises(InactiveAccountError):
            normalize([voucher])

    def test_unknown_checked_before_inactive_in_entry_order(self):
        # Entry 1 references an unknown account, entry 2 an inactive one:
        # the first entry's problem is reported.
        voucher = make_voucher(entries=[
            make_entry("9999", debit="1.00"),
            make_entry("5001", credit="1.00"),
        ])
        with self.assertRaises(UnknownAccountError):
            normalize([voucher])


class InvalidEntryAmountErrorTests(unittest.TestCase):
    """Amounts must be unsigned fixed-point strings, exactly one-sided."""

    def assert_amount_error(self, debit, credit):
        voucher = make_voucher(entries=[
            make_entry("1001", debit=debit, credit=credit),
            make_entry("4001", debit="0.00", credit="1.00"),
        ])
        with self.assertRaises(InvalidEntryAmountError):
            normalize([voucher])

    def test_valid_amount_boundaries(self):
        # Non-zero boundary shapes, used on the occurring side; the zero
        # side exercises the accepted zero spellings ("0", "0.0", "0.00").
        for amount in ("5", "5.4", "5.44", "00.10",
                       "123456789012345678901234567890.55"):
            for zero in ("0", "0.0", "0.00"):
                voucher = make_voucher(entries=[
                    make_entry("1001", debit=amount, credit=zero),
                    make_entry("4001", debit=zero, credit=amount),
                ])
                with self.subTest(amount=amount, zero=zero):
                    result = normalize([voucher])
                    self.assertEqual(result[0]["debit_total"],
                                     result[0]["credit_total"])

    def test_non_string_amounts(self):
        for bad in (1, 1.5, 0, True, False, None, ["1.00"], {"v": 1}):
            with self.subTest(bad=bad):
                self.assert_amount_error(bad, "0.00")

    def test_loose_parsing_lookalikes_rejected(self):
        for bad in ("-1.00",      # sign
                    "+1.00",      # sign
                    "1e3",        # exponent
                    "1E3",
                    "1,000.00",   # thousands separator
                    " 1.00",      # leading whitespace
                    "1.00 ",      # trailing whitespace
                    ".5",         # leading dot
                    "5.",         # trailing dot
                    "1.005",      # more than two decimals
                    "1.00.0",
                    "",
                    "abc",
                    "１２３",      # fullwidth digits are not [0-9]
                    ):
            with self.subTest(bad=bad):
                self.assert_amount_error(bad, "0.00")

    def test_both_sides_zero_rejected(self):
        self.assert_amount_error("0.00", "0.00")
        self.assert_amount_error("0", "0.0")

    def test_both_sides_nonzero_rejected(self):
        self.assert_amount_error("1.00", "1.00")
        self.assert_amount_error("0.01", "0.02")


class UnbalancedVoucherErrorTests(unittest.TestCase):
    def test_totals_must_match(self):
        voucher = make_voucher(entries=[
            make_entry("1001", debit="10.00"),
            make_entry("4001", credit="9.99"),
        ])
        with self.assertRaises(UnbalancedVoucherError):
            normalize([voucher])

    def test_one_cent_difference_is_unbalanced(self):
        voucher = make_voucher(entries=[
            make_entry("1001", debit="0.01"),
            make_entry("1002", debit="0.01"),
            make_entry("4001", credit="0.01"),
        ])
        with self.assertRaises(UnbalancedVoucherError):
            normalize([voucher])


class ValidationPriorityTests(unittest.TestCase):
    """Validation stops at the first problem; later errors must not
    mask the established priority order."""

    def test_format_error_before_duplicate_check(self):
        # Voucher #1 duplicates V-1's id but is also structurally broken:
        # the structural (phase 1) error wins.
        broken = make_voucher("V-1", date="not-a-date")
        with self.assertRaises(VoucherFormatError):
            normalize([make_voucher("V-1"), broken])

    def test_duplicate_before_currency_check(self):
        dupe = make_voucher("V-1", currency="EUR")
        with self.assertRaises(DuplicateVoucherError):
            normalize([make_voucher("V-1"), dupe])

    def test_currency_before_account_lookup(self):
        voucher = make_voucher(currency="EUR", entries=[
            make_entry("9999", debit="1.00"),
            make_entry("4001", credit="1.00"),
        ])
        with self.assertRaises(UnsupportedCurrencyError):
            normalize([voucher])

    def test_account_lookup_before_amount_parse(self):
        voucher = make_voucher(entries=[
            make_entry("9999", debit="not-an-amount"),
            make_entry("4001", credit="1.00"),
        ])
        with self.assertRaises(UnknownAccountError):
            normalize([voucher])

    def test_inactive_account_before_amount_parse(self):
        voucher = make_voucher(entries=[
            make_entry("5001", debit="-1.00"),
            make_entry("4001", credit="1.00"),
        ])
        with self.assertRaises(InactiveAccountError):
            normalize([voucher])

    def test_amount_error_before_balance_check(self):
        voucher = make_voucher(entries=[
            make_entry("1001", debit="bad"),
            make_entry("4001", credit="999.00"),
        ])
        with self.assertRaises(InvalidEntryAmountError):
            normalize([voucher])

    def test_first_failing_voucher_stops_the_batch(self):
        vouchers = [
            make_voucher("V-1"),
            make_voucher("V-2", entries=[           # unbalanced
                make_entry("1001", debit="1.00"),
                make_entry("4001", credit="2.00"),
            ]),
            make_voucher("V-3", date="bad-date"),   # would also fail
        ]
        with self.assertRaises(UnbalancedVoucherError) as ctx:
            normalize(vouchers)
        self.assertIn("voucher #1", str(ctx.exception))

    def test_first_failing_entry_stops_the_voucher(self):
        voucher = make_voucher(entries=[
            make_entry("1001", debit="1.00"),
            make_entry("9999", credit="1.00"),      # line 2: unknown
            make_entry("4001", credit="bad"),       # line 3: also bad
        ])
        with self.assertRaises(UnknownAccountError) as ctx:
            normalize([voucher])
        self.assertIn("line 2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
