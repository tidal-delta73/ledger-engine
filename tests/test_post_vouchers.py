"""Regression tests for ``ledger_engine.post_vouchers``.

The posting entry point shares the validation pipeline with
``normalize_vouchers`` and adds deterministic journal/balance assembly.
These tests lock the added contract: journal expansion order, per-account
turnovers and ending-balance side rules, zero rows for untouched and
inactive accounts, the shared first-failure behaviour, no-mutation and
byte-level reproducibility.

Only the Python standard library is used, so the suite runs on a plain
CPython 3.10+ install.
"""
from __future__ import annotations

import copy
import json
import unittest

from ledger_engine import (
    DuplicateVoucherError,
    InactiveAccountError,
    InvalidEntryAmountError,
    UnbalancedVoucherError,
    UnknownAccountError,
    UnsupportedCurrencyError,
    VoucherFormatError,
    normalize_vouchers,
    post_vouchers,
)

BASE = "CNY"


def make_chart():
    return [
        {"code": "1001", "name": "库存现金", "normal_side": "debit",
         "active": True},
        {"code": "2202", "name": "应付账款", "normal_side": "credit",
         "active": True},
        {"code": "6601", "name": "管理费用", "normal_side": "debit",
         "active": True},
        {"code": "4001", "name": "停用收入", "normal_side": "credit",
         "active": False},
    ]


def make_entry(account_code="1001", summary="业务", debit="0.00",
               credit="0.00"):
    return {
        "account_code": account_code,
        "summary": summary,
        "debit": debit,
        "credit": credit,
    }


def make_voucher(voucher_id="V-2024-001", date_text="2024-02-29",
                 currency=BASE, entries=None):
    if entries is None:
        entries = [
            make_entry("6601", "购办公用品", debit="100", credit="00"),
            make_entry("2202", "赊购入账", debit="0.0", credit="100.00"),
        ]
    return {
        "voucher_id": voucher_id,
        "date": date_text,
        "currency": currency,
        "entries": entries,
    }


def make_batch():
    return [
        make_voucher(),
        make_voucher(
            "V-2024-002",
            "2024-03-01",
            entries=[
                make_entry("2202", "偿还欠款", debit="40.5", credit="0"),
                make_entry("1001", "现金支付", debit="0", credit="40.50"),
            ],
        ),
    ]


def accounts_by_code(result):
    return {row["code"]: row for row in result["accounts"]}


class TestJournalShape(unittest.TestCase):
    def test_top_level_keys_and_base_currency(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        self.assertEqual(list(result), ["base_currency", "journal",
                                        "accounts"])
        self.assertEqual(result["base_currency"], BASE)

    def test_journal_expands_in_voucher_then_line_order(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        self.assertEqual(
            [(row["voucher_id"], row["line_no"]) for row in result["journal"]],
            [("V-2024-001", 1), ("V-2024-001", 2),
             ("V-2024-002", 1), ("V-2024-002", 2)],
        )

    def test_journal_row_fields_and_values(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        row = result["journal"][0]
        self.assertEqual(
            list(row),
            ["voucher_id", "date", "line_no", "account_code",
             "account_name", "summary", "debit", "credit"],
        )
        self.assertEqual(
            row,
            {"voucher_id": "V-2024-001", "date": "2024-02-29",
             "line_no": 1, "account_code": "6601",
             "account_name": "管理费用", "summary": "购办公用品",
             "debit": "100.00", "credit": "0.00"},
        )

    def test_same_account_rows_are_not_merged(self):
        voucher = make_voucher(entries=[
            make_entry("6601", "第一笔", debit="10", credit="0"),
            make_entry("6601", "第二笔", debit="20", credit="0"),
            make_entry("1001", "付款", debit="0", credit="30"),
        ])
        result = post_vouchers(BASE, make_chart(), [voucher])
        self.assertEqual(
            [(row["account_code"], row["summary"])
             for row in result["journal"]],
            [("6601", "第一笔"), ("6601", "第二笔"), ("1001", "付款")],
        )

    def test_journal_amounts_have_exactly_two_decimal_places(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        for row in result["journal"]:
            for field in ("debit", "credit"):
                self.assertRegex(row[field], r"^[0-9]+\.[0-9]{2}$")


class TestAccountSummaries(unittest.TestCase):
    def test_accounts_follow_chart_order_and_include_inactive(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        self.assertEqual([row["code"] for row in result["accounts"]],
                         ["1001", "2202", "6601", "4001"])

    def test_account_row_field_order(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        self.assertEqual(
            list(result["accounts"][0]),
            ["code", "name", "normal_side", "debit_turnover",
             "credit_turnover", "ending_side", "ending_balance"],
        )

    def test_turnovers_accumulate_across_vouchers(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        rows = accounts_by_code(result)
        self.assertEqual(
            rows["2202"],
            {"code": "2202", "name": "应付账款", "normal_side": "credit",
             "debit_turnover": "40.50", "credit_turnover": "100.00",
             "ending_side": "credit", "ending_balance": "59.50"},
        )
        self.assertEqual(
            rows["6601"],
            {"code": "6601", "name": "管理费用", "normal_side": "debit",
             "debit_turnover": "100.00", "credit_turnover": "0.00",
             "ending_side": "debit", "ending_balance": "100.00"},
        )

    def test_negative_net_flips_ending_side_to_opposite(self):
        voucher = make_voucher(entries=[
            make_entry("2202", "偿还", debit="60", credit="0"),
            make_entry("1001", "付款", debit="0", credit="60"),
        ])
        result = post_vouchers(BASE, make_chart(), [voucher])
        row = accounts_by_code(result)["2202"]
        self.assertEqual(row["ending_side"], "debit")
        self.assertEqual(row["ending_balance"], "60.00")

    def test_zero_net_keeps_normal_side(self):
        voucher = make_voucher(entries=[
            make_entry("6601", "借", debit="25", credit="0"),
            make_entry("6601", "冲回", debit="0", credit="10"),
            make_entry("6601", "再冲回", debit="0", credit="15"),
            make_entry("1001", "平衡", debit="0", credit="25"),
            make_entry("1001", "补平", debit="25", credit="0"),
        ])
        result = post_vouchers(BASE, make_chart(), [voucher])
        row = accounts_by_code(result)["6601"]
        self.assertEqual(row["ending_side"], "debit")
        self.assertEqual(row["ending_balance"], "0.00")

    def test_untouched_and_inactive_accounts_report_zero(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        rows = accounts_by_code(result)
        for code, side in (("4001", "credit"),):
            self.assertEqual(
                rows[code],
                {"code": code, "name": "停用收入", "normal_side": side,
                 "debit_turnover": "0.00", "credit_turnover": "0.00",
                 "ending_side": side, "ending_balance": "0.00"},
            )

    def test_empty_batch_gives_empty_journal_and_full_zero_accounts(self):
        result = post_vouchers(BASE, make_chart(), [])
        self.assertEqual(result["journal"], [])
        self.assertEqual(len(result["accounts"]), 4)
        for row in result["accounts"]:
            self.assertEqual(row["debit_turnover"], "0.00")
            self.assertEqual(row["credit_turnover"], "0.00")
            self.assertEqual(row["ending_side"], row["normal_side"])
            self.assertEqual(row["ending_balance"], "0.00")

    def test_huge_amounts_keep_full_integer_precision(self):
        big = "123456789012345678901234567890.01"
        voucher = make_voucher(entries=[
            make_entry("6601", "大额", debit=big, credit="0"),
            make_entry("2202", "大额", debit="0", credit=big),
        ])
        result = post_vouchers(BASE, make_chart(), [voucher])
        rows = accounts_by_code(result)
        self.assertEqual(rows["6601"]["ending_balance"], big)
        self.assertEqual(rows["2202"]["ending_balance"], big)


class TestSharedFailureSemantics(unittest.TestCase):
    def assert_first_failure(self, exc_type, batch):
        with self.assertRaises(exc_type):
            post_vouchers(BASE, make_chart(), batch)

    def test_duplicate_voucher_id(self):
        self.assert_first_failure(
            DuplicateVoucherError, [make_voucher(), make_voucher()])

    def test_currency_mismatch(self):
        self.assert_first_failure(
            UnsupportedCurrencyError,
            [make_voucher(currency="USD")])

    def test_unknown_account(self):
        self.assert_first_failure(
            UnknownAccountError,
            [make_voucher(entries=[
                make_entry("9999", "未知", debit="1", credit="0"),
                make_entry("1001", "平衡", debit="0", credit="1"),
            ])])

    def test_inactive_account(self):
        self.assert_first_failure(
            InactiveAccountError,
            [make_voucher(entries=[
                make_entry("4001", "停用", debit="1", credit="0"),
                make_entry("1001", "平衡", debit="0", credit="1"),
            ])])

    def test_invalid_amount(self):
        self.assert_first_failure(
            InvalidEntryAmountError,
            [make_voucher(entries=[
                make_entry("6601", "坏金额", debit="1.005", credit="0"),
                make_entry("1001", "平衡", debit="0", credit="1"),
            ])])

    def test_unbalanced_voucher(self):
        self.assert_first_failure(
            UnbalancedVoucherError,
            [make_voucher(entries=[
                make_entry("6601", "借", debit="2", credit="0"),
                make_entry("1001", "贷", debit="0", credit="1"),
            ])])

    def test_structural_defect_still_a_format_error(self):
        self.assert_first_failure(VoucherFormatError, [{"voucher_id": "X"}])

    def test_failure_raises_before_any_result_and_matches_normalize(self):
        bad_batch = [make_voucher(), make_voucher()]
        for entry_point in (normalize_vouchers, post_vouchers):
            with self.assertRaises(DuplicateVoucherError) as caught:
                entry_point(BASE, make_chart(), bad_batch)
            if entry_point is normalize_vouchers:
                expected = str(caught.exception)
            else:
                self.assertEqual(str(caught.exception), expected)


class TestDeterminismAndIsolation(unittest.TestCase):
    def test_result_is_json_serializable_and_round_trips(self):
        result = post_vouchers(BASE, make_chart(), make_batch())
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_repeated_calls_are_byte_identical_and_fresh(self):
        first = post_vouchers(BASE, make_chart(), make_batch())
        second = post_vouchers(BASE, make_chart(), make_batch())
        kwargs = {"ensure_ascii": False, "sort_keys": False}
        self.assertEqual(json.dumps(first, **kwargs),
                         json.dumps(second, **kwargs))
        self.assertIsNot(first, second)
        self.assertIsNot(first["journal"], second["journal"])
        self.assertIsNot(first["accounts"], second["accounts"])
        if first["journal"]:
            self.assertIsNot(first["journal"][0], second["journal"][0])
        self.assertIsNot(first["accounts"][0], second["accounts"][0])

    def test_inputs_are_not_mutated(self):
        chart = make_chart()
        batch = make_batch()
        chart_snapshot = copy.deepcopy(chart)
        batch_snapshot = copy.deepcopy(batch)
        post_vouchers(BASE, chart, batch)
        self.assertEqual(chart, chart_snapshot)
        self.assertEqual(batch, batch_snapshot)

    def test_mutating_result_does_not_leak_into_next_call(self):
        first = post_vouchers(BASE, make_chart(), make_batch())
        first["journal"].clear()
        first["accounts"][0]["ending_balance"] = "corrupted"
        second = post_vouchers(BASE, make_chart(), make_batch())
        self.assertEqual(len(second["journal"]), 4)
        self.assertNotEqual(second["accounts"][0]["ending_balance"],
                            "corrupted")

    def test_extra_input_fields_are_ignored(self):
        chart = make_chart()
        chart[0]["extra"] = "ignored"
        voucher = make_voucher()
        voucher["memo"] = "ignored"
        voucher["entries"][0]["tag"] = "ignored"
        result = post_vouchers(BASE, chart, [voucher])
        self.assertEqual(list(result), ["base_currency", "journal",
                                        "accounts"])
        self.assertNotIn("memo", result["journal"][0])
        self.assertNotIn("tag", result["journal"][0])
        self.assertNotIn("extra", result["accounts"][0])


if __name__ == "__main__":
    unittest.main()
