"""Unit tests for the internal pipeline stages of ``ledger_engine``.

The public contract of ``normalize_vouchers`` is locked by
``test_normalize_vouchers.py``; this file instead verifies that each
refactored stage — chart parsing, structural validation, entry business
rules, integer-cent arithmetic and result construction — is usable and
correct in isolation, so later features (posting, reversal,
recalculation) can reuse the same deterministic rules.
"""
from __future__ import annotations

import unittest

from ledger_engine import (
    ChartOfAccountsError,
    InactiveAccountError,
    InvalidEntryAmountError,
    UnbalancedVoucherError,
    UnknownAccountError,
    VoucherFormatError,
)
from ledger_engine._amounts import format_cents, parse_amount_cents
from ledger_engine._chart import parse_chart, validate_base_currency
from ledger_engine._entries import validate_entry_business
from ledger_engine._results import build_entry_result, build_voucher_result
from ledger_engine._structure import validate_voucher_structure

CHART = [
    {"code": "1001", "name": "库存现金", "normal_side": "debit",
     "active": True},
    {"code": "4001", "name": "停用收入", "normal_side": "credit",
     "active": False},
]


def make_entry(account_code="1001", summary="业务", debit="1.00",
               credit="0.00"):
    return {
        "account_code": account_code,
        "summary": summary,
        "debit": debit,
        "credit": credit,
    }


def make_voucher(entries=None):
    return {
        "voucher_id": "V-1",
        "date": "2024-02-29",
        "currency": "CNY",
        "entries": entries if entries is not None else [make_entry(),
                                                        make_entry()],
    }


class AmountTests(unittest.TestCase):
    def test_parse_accepts_fixed_point_forms(self):
        for text, cents in (("0", 0), ("00", 0), ("0.0", 0), ("0.00", 0),
                            ("1", 100), ("1.2", 120), ("1.23", 123),
                            ("99999999999999999999.99",
                             9999999999999999999999)):
            with self.subTest(text=text):
                self.assertEqual(parse_amount_cents(text, where="t"), cents)

    def test_parse_rejects_loose_forms(self):
        for bad in ("-1", "+1", "1e3", "1,000.00", " 1", "1 ", ".5", "1.",
                    "1.234", "", "nan", 0, 1, 1.5, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidEntryAmountError):
                    parse_amount_cents(bad, where="t")

    def test_format_round_trips_through_parse(self):
        for cents in (0, 1, 10, 99, 100, 101, 12345678901234567890):
            with self.subTest(cents=cents):
                text = format_cents(cents)
                self.assertEqual(parse_amount_cents(text, where="t"), cents)

    def test_format_always_has_two_places(self):
        self.assertEqual(format_cents(0), "0.00")
        self.assertEqual(format_cents(5), "0.05")
        self.assertEqual(format_cents(120), "1.20")


class ChartTests(unittest.TestCase):
    def test_base_currency_boundary(self):
        validate_base_currency("CNY")  # must not raise
        for bad in ("cny", "CN", "CNYX", "", None, 123, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ChartOfAccountsError):
                    validate_base_currency(bad)

    def test_parse_chart_returns_exact_match_snapshots(self):
        accounts = parse_chart(CHART)
        self.assertEqual(list(accounts), ["1001", "4001"])
        self.assertEqual(accounts["1001"],
                         {"code": "1001", "name": "库存现金",
                          "normal_side": "debit", "active": True})
        # Snapshots are copies: mutating one must not touch the source.
        accounts["1001"]["name"] = "改名"
        self.assertEqual(CHART[0]["name"], "库存现金")

    def test_parse_chart_rejects_duplicates_and_bad_rows(self):
        with self.assertRaises(ChartOfAccountsError):
            parse_chart(CHART + [dict(CHART[0])])
        with self.assertRaises(ChartOfAccountsError):
            parse_chart("not-a-list")


class StructureTests(unittest.TestCase):
    def test_valid_voucher_returns_public_fields(self):
        voucher = make_voucher()
        voucher_id, date_text, currency, entries = \
            validate_voucher_structure(voucher, where="voucher #0")
        self.assertEqual((voucher_id, date_text, currency),
                         ("V-1", "2024-02-29", "CNY"))
        self.assertIs(entries, voucher["entries"])

    def test_extra_fields_are_ignored(self):
        voucher = make_voucher()
        voucher["extra"] = {"nested": [1, 2, 3]}
        voucher["entries"][0]["memo"] = "ignored"
        voucher_id, _, _, _ = validate_voucher_structure(
            voucher, where="voucher #0")
        self.assertEqual(voucher_id, "V-1")

    def test_all_entries_checked_before_return(self):
        voucher = make_voucher(entries=[make_entry(),
                                        make_entry(summary=None)])
        with self.assertRaises(VoucherFormatError):
            validate_voucher_structure(voucher, where="voucher #0")

    def test_bad_calendar_date_rejected(self):
        voucher = make_voucher()
        voucher["date"] = "2023-02-29"
        with self.assertRaises(VoucherFormatError):
            validate_voucher_structure(voucher, where="voucher #0")


class EntryBusinessTests(unittest.TestCase):
    def setUp(self):
        self.accounts = parse_chart(CHART)

    def test_resolves_account_and_amounts(self):
        account, debit, credit = validate_entry_business(
            make_entry(debit="1.20"), self.accounts, where="line 1")
        self.assertIs(account, self.accounts["1001"])
        self.assertEqual((debit, credit), (120, 0))

    def test_unknown_then_inactive_then_amount_precedence(self):
        with self.assertRaises(UnknownAccountError):
            validate_entry_business(make_entry("9999", debit="bad"),
                                    self.accounts, where="line 1")
        with self.assertRaises(InactiveAccountError):
            validate_entry_business(make_entry("4001", debit="bad"),
                                    self.accounts, where="line 1")
        with self.assertRaises(InvalidEntryAmountError):
            validate_entry_business(make_entry("1001", debit="bad"),
                                    self.accounts, where="line 1")

    def test_exactly_one_side_required(self):
        for debit, credit in (("0.00", "0.00"), ("1.00", "1.00")):
            with self.subTest(debit=debit, credit=credit):
                with self.assertRaises(InvalidEntryAmountError):
                    validate_entry_business(
                        make_entry(debit=debit, credit=credit),
                        self.accounts, where="line 1")


class ResultConstructionTests(unittest.TestCase):
    def test_entry_result_field_order_and_format(self):
        account = parse_chart(CHART)["1001"]
        result = build_entry_result(line_no=1, account=account,
                                    summary="业务", debit_cents=120,
                                    credit_cents=0)
        self.assertEqual(list(result),
                         ["line_no", "account_code", "account_name",
                          "normal_side", "summary", "debit", "credit"])
        self.assertEqual(result["debit"], "1.20")
        self.assertEqual(result["credit"], "0.00")

    def test_voucher_result_field_order_and_totals(self):
        result = build_voucher_result(
            voucher_id="V-1", date_text="2024-02-29", currency="CNY",
            entry_results=[], debit_total_cents=10000,
            credit_total_cents=10000, where="voucher #0 (V-1)")
        self.assertEqual(list(result),
                         ["voucher_id", "date", "currency", "entries",
                          "debit_total", "credit_total"])
        self.assertEqual(result["debit_total"], "100.00")

    def test_unbalanced_totals_rejected(self):
        with self.assertRaises(UnbalancedVoucherError):
            build_voucher_result(
                voucher_id="V-1", date_text="2024-02-29", currency="CNY",
                entry_results=[], debit_total_cents=100,
                credit_total_cents=99, where="voucher #0 (V-1)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
