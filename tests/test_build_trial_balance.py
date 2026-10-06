"""Regression tests for ``ledger_engine.build_trial_balance``.

The trial-balance entry point shares the validation pipeline with
``normalize_vouchers``/``post_vouchers`` and adds deterministic
per-account turnover/ending rows plus a totals summary.  These tests
lock the added contract: chart-order rows (including untouched and
inactive accounts), the debit-minus-credit ending-side rule that is
independent of ``normal_side``, zero ``0.00``/``0.00`` ending columns on
a zero net, the four-column totals and both balance flags, the
deterministic zero summary for empty batches and empty charts, the
shared first-failure behaviour (leaf type *and* message identical to
``normalize_vouchers``), no-mutation and byte-level reproducibility.

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
    build_trial_balance,
    normalize_vouchers,
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
        {"code": "6001", "name": "主营业务收入", "normal_side": "credit",
         "active": True},
        {"code": "1901", "name": "未使用科目", "normal_side": "debit",
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


ZERO_TOTALS = {
    "debit_turnover": "0.00",
    "credit_turnover": "0.00",
    "ending_debit": "0.00",
    "ending_credit": "0.00",
    "turnover_balanced": True,
    "ending_balanced": True,
}


class TestTrialBalanceShape(unittest.TestCase):
    def test_top_level_keys_and_base_currency(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        self.assertEqual(list(result),
                         ["base_currency", "accounts", "totals"])
        self.assertEqual(result["base_currency"], BASE)

    def test_accounts_follow_chart_order_and_include_inactive(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        self.assertEqual([row["code"] for row in result["accounts"]],
                         ["1001", "2202", "6601", "6001", "1901", "4001"])

    def test_account_row_field_order(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        self.assertEqual(
            list(result["accounts"][0]),
            ["code", "name", "normal_side", "debit_turnover",
             "credit_turnover", "ending_debit", "ending_credit"],
        )

    def test_totals_field_order(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        self.assertEqual(
            list(result["totals"]),
            ["debit_turnover", "credit_turnover", "ending_debit",
             "ending_credit", "turnover_balanced", "ending_balanced"],
        )

    def test_every_amount_string_has_exactly_two_decimal_places(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        two_places = r"^[0-9]+\.[0-9]{2}$"
        for row in result["accounts"]:
            for field in ("debit_turnover", "credit_turnover",
                          "ending_debit", "ending_credit"):
                self.assertRegex(row[field], two_places)
        for field in ("debit_turnover", "credit_turnover",
                      "ending_debit", "ending_credit"):
            self.assertRegex(result["totals"][field], two_places)


class TestTurnoverAndEndingRows(unittest.TestCase):
    def test_turnovers_accumulate_across_vouchers(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        rows = accounts_by_code(result)
        self.assertEqual(rows["2202"]["debit_turnover"], "40.50")
        self.assertEqual(rows["2202"]["credit_turnover"], "100.00")
        self.assertEqual(rows["6601"]["debit_turnover"], "100.00")
        self.assertEqual(rows["6601"]["credit_turnover"], "0.00")

    def test_net_lands_on_one_side_ignoring_normal_side(self):
        # 2202 is credit-normal but has a credit net -> ending_credit.
        result = build_trial_balance(BASE, make_chart(), make_batch())
        liability = accounts_by_code(result)["2202"]
        self.assertEqual(liability["ending_debit"], "0.00")
        self.assertEqual(liability["ending_credit"], "59.50")
        # 6601 is debit-normal with a debit net -> ending_debit.
        expense = accounts_by_code(result)["6601"]
        self.assertEqual(expense["ending_debit"], "100.00")
        self.assertEqual(expense["ending_credit"], "0.00")

    def test_debit_net_on_credit_normal_account_flips_to_ending_debit(self):
        # 6001 is credit-normal yet used more on the debit side.
        voucher = make_voucher(entries=[
            make_entry("6001", "红字冲回", debit="80", credit="0"),
            make_entry("6001", "确认收入", debit="0", credit="30"),
            make_entry("1001", "平衡", debit="0", credit="50"),
        ])
        result = build_trial_balance(BASE, make_chart(), [voucher])
        row = accounts_by_code(result)["6001"]
        self.assertEqual(row["debit_turnover"], "80.00")
        self.assertEqual(row["credit_turnover"], "30.00")
        self.assertEqual(row["ending_debit"], "50.00")
        self.assertEqual(row["ending_credit"], "0.00")

    def test_credit_net_on_debit_normal_account_goes_to_ending_credit(self):
        # 1001 is debit-normal but only ever credited.
        voucher = make_voucher(entries=[
            make_entry("6601", "费用", debit="60", credit="0"),
            make_entry("1001", "付现", debit="0", credit="60"),
        ])
        result = build_trial_balance(BASE, make_chart(), [voucher])
        row = accounts_by_code(result)["1001"]
        self.assertEqual(row["ending_debit"], "0.00")
        self.assertEqual(row["ending_credit"], "60.00")

    def test_zero_net_reports_zero_on_both_ending_columns(self):
        voucher = make_voucher(entries=[
            make_entry("6601", "借", debit="25", credit="0"),
            make_entry("6601", "冲回", debit="0", credit="10"),
            make_entry("6601", "再冲回", debit="0", credit="15"),
            make_entry("1001", "平衡", debit="0", credit="25"),
            make_entry("1001", "补平", debit="25", credit="0"),
        ])
        result = build_trial_balance(BASE, make_chart(), [voucher])
        row = accounts_by_code(result)["6601"]
        self.assertEqual(row["debit_turnover"], "25.00")
        self.assertEqual(row["credit_turnover"], "25.00")
        self.assertEqual(row["ending_debit"], "0.00")
        self.assertEqual(row["ending_credit"], "0.00")

    def test_untouched_and_inactive_accounts_report_full_zero_rows(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        rows = accounts_by_code(result)
        for code, side in (("1901", "debit"), ("4001", "credit")):
            self.assertEqual(
                rows[code],
                {"code": code,
                 "name": "未使用科目" if code == "1901" else "停用收入",
                 "normal_side": side,
                 "debit_turnover": "0.00", "credit_turnover": "0.00",
                 "ending_debit": "0.00", "ending_credit": "0.00"},
            )

    def test_huge_amounts_keep_full_integer_precision(self):
        big = "123456789012345678901234567890.01"
        voucher = make_voucher(entries=[
            make_entry("6601", "大额", debit=big, credit="0"),
            make_entry("2202", "大额", debit="0", credit=big),
        ])
        result = build_trial_balance(BASE, make_chart(), [voucher])
        rows = accounts_by_code(result)
        self.assertEqual(rows["6601"]["ending_debit"], big)
        self.assertEqual(rows["6601"]["ending_credit"], "0.00")
        self.assertEqual(rows["2202"]["ending_debit"], "0.00")
        self.assertEqual(rows["2202"]["ending_credit"], big)


class TestTotals(unittest.TestCase):
    def test_totals_sum_the_four_columns_and_balance(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        totals = result["totals"]
        # Turnovers: 100.00 + 40.50 = 140.50 on each side.
        self.assertEqual(totals["debit_turnover"], "140.50")
        self.assertEqual(totals["credit_turnover"], "140.50")
        # Ending: 6601 debit 100.00 vs (2202 credit 59.50 + 1001 credit
        # 40.50) = 100.00.
        self.assertEqual(totals["ending_debit"], "100.00")
        self.assertEqual(totals["ending_credit"], "100.00")
        self.assertIs(totals["turnover_balanced"], True)
        self.assertIs(totals["ending_balanced"], True)

    def test_empty_batch_gives_zero_rows_and_zero_totals(self):
        result = build_trial_balance(BASE, make_chart(), [])
        self.assertEqual(len(result["accounts"]), 6)
        for row in result["accounts"]:
            self.assertEqual(row["debit_turnover"], "0.00")
            self.assertEqual(row["credit_turnover"], "0.00")
            self.assertEqual(row["ending_debit"], "0.00")
            self.assertEqual(row["ending_credit"], "0.00")
        self.assertEqual(result["totals"], ZERO_TOTALS)

    def test_empty_chart_and_empty_batch_gives_zero_summary(self):
        result = build_trial_balance(BASE, [], [])
        self.assertEqual(result["base_currency"], BASE)
        self.assertEqual(result["accounts"], [])
        self.assertEqual(result["totals"], ZERO_TOTALS)

    def test_huge_amounts_totals_keep_full_precision_and_stay_balanced(self):
        big = "9007199254740993.00"  # beyond 2**53
        voucher = make_voucher(entries=[
            make_entry("6601", "大额", debit=big, credit="0"),
            make_entry("2202", "大额", debit="0", credit=big),
        ])
        result = build_trial_balance(BASE, make_chart(), [voucher])
        totals = result["totals"]
        self.assertEqual(totals["debit_turnover"], big)
        self.assertEqual(totals["credit_turnover"], big)
        self.assertEqual(totals["ending_debit"], big)
        self.assertEqual(totals["ending_credit"], big)
        self.assertIs(totals["turnover_balanced"], True)
        self.assertIs(totals["ending_balanced"], True)


class TestSharedFailureSemantics(unittest.TestCase):
    def assert_same_failure_as_normalize(self, exc_type, cur, chart, batch):
        """build_trial_balance must raise the same leaf type and message."""
        with self.assertRaises(exc_type) as built:
            build_trial_balance(cur, copy.deepcopy(chart),
                                copy.deepcopy(batch))
        with self.assertRaises(exc_type) as normalized:
            normalize_vouchers(cur, copy.deepcopy(chart),
                               copy.deepcopy(batch))
        self.assertEqual(str(built.exception), str(normalized.exception))

    def test_duplicate_voucher_id(self):
        self.assert_same_failure_as_normalize(
            DuplicateVoucherError, BASE, make_chart(),
            [make_voucher(), make_voucher()])

    def test_currency_mismatch(self):
        self.assert_same_failure_as_normalize(
            UnsupportedCurrencyError, BASE, make_chart(),
            [make_voucher(currency="USD")])

    def test_unknown_account(self):
        self.assert_same_failure_as_normalize(
            UnknownAccountError, BASE, make_chart(),
            [make_voucher(entries=[
                make_entry("9999", "未知", debit="1", credit="0"),
                make_entry("1001", "平衡", debit="0", credit="1"),
            ])])

    def test_inactive_account(self):
        self.assert_same_failure_as_normalize(
            InactiveAccountError, BASE, make_chart(),
            [make_voucher(entries=[
                make_entry("4001", "停用", debit="1", credit="0"),
                make_entry("1001", "平衡", debit="0", credit="1"),
            ])])

    def test_invalid_amount(self):
        self.assert_same_failure_as_normalize(
            InvalidEntryAmountError, BASE, make_chart(),
            [make_voucher(entries=[
                make_entry("6601", "坏金额", debit="1.005", credit="0"),
                make_entry("1001", "平衡", debit="0", credit="1"),
            ])])

    def test_unbalanced_voucher(self):
        self.assert_same_failure_as_normalize(
            UnbalancedVoucherError, BASE, make_chart(),
            [make_voucher(entries=[
                make_entry("6601", "借", debit="2", credit="0"),
                make_entry("1001", "贷", debit="0", credit="1"),
            ])])

    def test_structural_defect_still_a_format_error(self):
        self.assert_same_failure_as_normalize(
            VoucherFormatError, BASE, make_chart(),
            [{"voucher_id": "X"}])

    def test_no_partial_result_on_failure(self):
        with self.assertRaises(DuplicateVoucherError):
            build_trial_balance(
                BASE, make_chart(), [make_voucher(), make_voucher()])


def _reversed_key_order(value):
    """Deep copy with every dict's key insertion order reversed."""
    if isinstance(value, dict):
        return {key: _reversed_key_order(val)
                for key, val in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reversed_key_order(item) for item in value]
    return value


def _mutable_ids(value):
    found = set()

    def walk(node):
        if isinstance(node, (dict, list)):
            found.add(id(node))
            children = node.values() if isinstance(node, dict) else node
            for child in children:
                walk(child)

    walk(value)
    return found


class TestDeterminismAndIsolation(unittest.TestCase):
    JSON_KW = {"ensure_ascii": False, "sort_keys": False}

    def test_result_is_json_serializable_and_round_trips(self):
        result = build_trial_balance(BASE, make_chart(), make_batch())
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_repeated_calls_are_byte_identical_and_fresh(self):
        first = build_trial_balance(BASE, make_chart(), make_batch())
        second = build_trial_balance(BASE, make_chart(), make_batch())
        self.assertEqual(json.dumps(first, **self.JSON_KW),
                         json.dumps(second, **self.JSON_KW))
        self.assertIsNot(first, second)
        self.assertIsNot(first["accounts"], second["accounts"])
        self.assertIsNot(first["totals"], second["totals"])
        self.assertIsNot(first["accounts"][0], second["accounts"][0])

    def test_equivalent_inputs_regardless_of_dict_insertion_order(self):
        first = build_trial_balance(BASE, make_chart(), make_batch())
        second = build_trial_balance(
            BASE, _reversed_key_order(make_chart()),
            _reversed_key_order(make_batch()))
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, **self.JSON_KW),
                         json.dumps(second, **self.JSON_KW))
        self.assertTrue(
            _mutable_ids(first).isdisjoint(_mutable_ids(second)))

    def test_inputs_are_not_mutated(self):
        chart = make_chart()
        batch = make_batch()
        chart_snapshot = copy.deepcopy(chart)
        batch_snapshot = copy.deepcopy(batch)
        build_trial_balance(BASE, chart, batch)
        self.assertEqual(chart, chart_snapshot)
        self.assertEqual(batch, batch_snapshot)

    def test_mutating_result_does_not_leak_into_next_call(self):
        first = build_trial_balance(BASE, make_chart(), make_batch())
        first["accounts"].clear()
        first["totals"]["ending_debit"] = "corrupted"
        first["totals"]["turnover_balanced"] = False
        second = build_trial_balance(BASE, make_chart(), make_batch())
        self.assertEqual(len(second["accounts"]), 6)
        self.assertNotEqual(second["totals"]["ending_debit"], "corrupted")
        self.assertIs(second["totals"]["turnover_balanced"], True)

    def test_extra_input_fields_are_ignored(self):
        chart = make_chart()
        chart[0]["extra"] = "ignored"
        voucher = make_voucher()
        voucher["memo"] = "ignored"
        voucher["entries"][0]["tag"] = "ignored"
        result = build_trial_balance(BASE, chart, [voucher])
        self.assertEqual(list(result),
                         ["base_currency", "accounts", "totals"])
        self.assertNotIn("extra", result["accounts"][0])
        self.assertNotIn("memo", result)


if __name__ == "__main__":
    unittest.main()
