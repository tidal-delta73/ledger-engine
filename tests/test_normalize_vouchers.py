"""Regression tests for ``ledger_engine.normalize_vouchers``.

The public entry point validates and normalizes a batch of single-currency
raw vouchers.  These tests lock the contract without adding any business
interface: exception categories, validation order (the first problem wins),
exact string comparison, fixed-point arithmetic, ordering, serialization
determinism and the no-mutation guarantee.

Only the Python standard library is used, so the suite runs on a plain
CPython 3.10+ install and does not depend on the current working directory,
a timezone, dict/set iteration order, or third-party packages.
"""
from __future__ import annotations

import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ledger_engine import (
    ChartOfAccountsError,
    DuplicateVoucherError,
    InactiveAccountError,
    InvalidEntryAmountError,
    LedgerEngineError,
    UnsupportedCurrencyError,
    UnbalancedVoucherError,
    UnknownAccountError,
    VoucherFormatError,
    __version__,
    normalize_vouchers,
)
from ledger_engine.__main__ import main

BASE = "CNY"


# --------------------------------------------------------------------- #
# Fresh-object factories.  Every call returns brand-new containers so a
# test can never observe mutation leaking from a previous test.
# --------------------------------------------------------------------- #
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


def make_valid_batch():
    second = make_voucher(
        "V-2024-002", "2023-03-01",
        entries=[
            make_entry("1001", "支付欠款", debit="1234.5", credit="0.00"),
            make_entry("2202", "冲减应付", debit="0", credit="1200.00"),
            make_entry("6601", "费用调整", debit="0.00", credit="34.5"),
        ],
    )
    return [make_voucher(), second]


EXPECTED_BATCH = [
    {
        "voucher_id": "V-2024-001",
        "date": "2024-02-29",
        "currency": "CNY",
        "entries": [
            {"line_no": 1, "account_code": "6601", "account_name": "管理费用",
             "normal_side": "debit", "summary": "购办公用品",
             "debit": "100.00", "credit": "0.00"},
            {"line_no": 2, "account_code": "2202", "account_name": "应付账款",
             "normal_side": "credit", "summary": "赊购入账",
             "debit": "0.00", "credit": "100.00"},
        ],
        "debit_total": "100.00",
        "credit_total": "100.00",
    },
    {
        "voucher_id": "V-2024-002",
        "date": "2023-03-01",
        "currency": "CNY",
        "entries": [
            {"line_no": 1, "account_code": "1001", "account_name": "库存现金",
             "normal_side": "debit", "summary": "支付欠款",
             "debit": "1234.50", "credit": "0.00"},
            {"line_no": 2, "account_code": "2202", "account_name": "应付账款",
             "normal_side": "credit", "summary": "冲减应付",
             "debit": "0.00", "credit": "1200.00"},
            {"line_no": 3, "account_code": "6601", "account_name": "管理费用",
             "normal_side": "debit", "summary": "费用调整",
             "debit": "0.00", "credit": "34.50"},
        ],
        "debit_total": "1234.50",
        "credit_total": "1234.50",
    },
]

# Fixed serialization parameters: key order, separators and UTF-8 output are
# pinned explicitly, so the bytes below cannot drift with environment.
JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}
GOLDEN_JSON = (
    '[{"credit_total":"100.00","currency":"CNY","date":"2024-02-29",'
    '"debit_total":"100.00","entries":[{"account_code":"6601",'
    '"account_name":"管理费用","credit":"0.00","debit":"100.00",'
    '"line_no":1,"normal_side":"debit","summary":"购办公用品"},'
    '{"account_code":"2202","account_name":"应付账款","credit":"100.00",'
    '"debit":"0.00","line_no":2,"normal_side":"credit",'
    '"summary":"赊购入账"}],"voucher_id":"V-2024-001"},'
    '{"credit_total":"1234.50","currency":"CNY","date":"2023-03-01",'
    '"debit_total":"1234.50","entries":[{"account_code":"1001",'
    '"account_name":"库存现金","credit":"0.00","debit":"1234.50",'
    '"line_no":1,"normal_side":"debit","summary":"支付欠款"},'
    '{"account_code":"2202","account_name":"应付账款","credit":"1200.00",'
    '"debit":"0.00","line_no":2,"normal_side":"credit",'
    '"summary":"冲减应付"},{"account_code":"6601",'
    '"account_name":"管理费用","credit":"34.50","debit":"0.00",'
    '"line_no":3,"normal_side":"debit","summary":"费用调整"}],'
    '"voucher_id":"V-2024-002"}]'
)

LEAF_ERRORS = (
    ChartOfAccountsError,
    VoucherFormatError,
    DuplicateVoucherError,
    UnsupportedCurrencyError,
    UnknownAccountError,
    InactiveAccountError,
    InvalidEntryAmountError,
    UnbalancedVoucherError,
)


class NormalizeTestCase(unittest.TestCase):
    """Assert the *exact* leaf class, not merely a base class."""

    def assert_normalizes(self, base, chart, vouchers, expected):
        result = normalize_vouchers(base, chart, vouchers)
        self.assertEqual(result, expected)
        return result

    def assert_rejects(self, expected_exc, base, chart, vouchers):
        """Call must raise exactly expected_exc and leave all inputs alone."""
        snapshot = copy.deepcopy((base, chart, vouchers))
        with self.assertRaises(expected_exc) as caught:
            normalize_vouchers(base, chart, vouchers)
        self.assertIs(type(caught.exception), expected_exc,
                      "unique public category must be the leaf class")
        self.assertEqual((base, chart, vouchers), snapshot,
                         "failed validation must not mutate its inputs")
        return caught.exception


# --------------------------------------------------------------------- #
# Success path
# --------------------------------------------------------------------- #
class NormalizationTests(NormalizeTestCase):
    def test_valid_batch_normalizes_to_expected_contract(self):
        self.assert_normalizes(BASE, make_chart(), make_valid_batch(),
                               EXPECTED_BATCH)

    def test_voucher_and_entry_order_is_preserved(self):
        result = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        self.assertEqual([v["voucher_id"] for v in result],
                         ["V-2024-001", "V-2024-002"])
        self.assertEqual(
            [e["account_code"] for e in result[0]["entries"]],
            ["6601", "2202"],
        )
        self.assertEqual(
            [e["account_code"] for e in result[1]["entries"]],
            ["1001", "2202", "6601"],
        )

    def test_line_numbers_start_at_one_and_are_sequential(self):
        result = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        for voucher in result:
            self.assertEqual([e["line_no"] for e in voucher["entries"]],
                             list(range(1, len(voucher["entries"]) + 1)))

    def test_all_amounts_have_exactly_two_decimal_places(self):
        result = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        for voucher in result:
            for entry in voucher["entries"]:
                self.assertRegex(entry["debit"], r"^[0-9]+\.[0-9]{2}$")
                self.assertRegex(entry["credit"], r"^[0-9]+\.[0-9]{2}$")

    def test_zero_forms_all_normalize_to_0_00(self):
        # "00" and "0.0" occur in the first voucher, "0"/"0.00" in the second.
        result = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        zeros = [
            result[0]["entries"][0]["credit"],   # raw "00"
            result[0]["entries"][1]["debit"],    # raw "0.0"
            result[1]["entries"][1]["debit"],    # raw "0"
            result[1]["entries"][0]["credit"],   # raw "0.00"
        ]
        self.assertEqual(zeros, ["0.00"] * 4)

    def test_single_fraction_digit_is_padded_to_two(self):
        result = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        self.assertEqual(result[1]["entries"][0]["debit"], "1234.50")
        self.assertEqual(result[1]["entries"][2]["credit"], "34.50")

    def test_integer_strings_get_decimal_suffix(self):
        result = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        self.assertEqual(result[0]["entries"][0]["debit"], "100.00")
        self.assertEqual(result[0]["entries"][1]["credit"], "100.00")

    def test_totals_have_two_decimal_places(self):
        result = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        for voucher in result:
            self.assertRegex(voucher["debit_total"], r"^[0-9]+\.[0-9]{2}$")
            self.assertRegex(voucher["credit_total"], r"^[0-9]+\.[0-9]{2}$")
        self.assertEqual(result[0]["debit_total"], "100.00")
        self.assertEqual(result[1]["debit_total"], "1234.50")

    def test_account_name_and_normal_side_come_from_chart(self):
        chart = make_chart()
        # 2202 is a credit-side account used on the debit side here; the
        # reported normal_side is still the chart's value, untouched.
        result = normalize_vouchers(BASE, chart, make_valid_batch())
        by_code = {a["code"]: a for a in chart}
        for voucher in result:
            for entry in voucher["entries"]:
                source = by_code[entry["account_code"]]
                self.assertEqual(entry["account_name"], source["name"])
                self.assertEqual(entry["normal_side"], source["normal_side"])
        self.assertEqual(result[0]["entries"][1]["normal_side"], "credit")

    def test_result_is_json_serializable_and_round_trips(self):
        result = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        encoded = json.dumps(result)  # default kwargs must work
        self.assertEqual(json.loads(encoded), result)

    def test_repeated_calls_return_equal_objects(self):
        first = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        second = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        self.assertEqual(first, second)
        self.assertEqual(first, EXPECTED_BATCH)
        self.assertIsNot(first, second)

    def test_fixed_json_kwargs_produce_byte_identical_text(self):
        first = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        second = normalize_vouchers(BASE, make_chart(), make_valid_batch())
        text_a = json.dumps(first, **JSON_KW)
        text_b = json.dumps(second, **JSON_KW)
        self.assertEqual(text_a, text_b)
        self.assertEqual(text_a, GOLDEN_JSON)
        self.assertEqual(text_a.encode("utf-8"), GOLDEN_JSON.encode("utf-8"))

    def test_empty_voucher_batch_is_valid(self):
        result = normalize_vouchers(BASE, make_chart(), [])
        self.assertEqual(result, [])
        self.assertEqual(json.dumps(result, **JSON_KW), "[]")

    def test_inputs_not_mutated_on_success(self):
        chart = make_chart()
        vouchers = make_valid_batch()
        snapshot = copy.deepcopy((BASE, chart, vouchers))
        normalize_vouchers(BASE, chart, vouchers)
        self.assertEqual((BASE, chart, vouchers), snapshot)
        # Raw amount strings stay unnormalized in the caller's objects.
        self.assertEqual(vouchers[0]["entries"][0]["debit"], "100")
        self.assertEqual(vouchers[0]["entries"][0]["credit"], "00")
        self.assertNotIn("line_no", vouchers[0]["entries"][0])
        self.assertNotIn("debit_total", vouchers[0])

    def test_returned_containers_are_new_objects(self):
        chart = make_chart()
        vouchers = make_valid_batch()
        result = normalize_vouchers(BASE, chart, vouchers)
        self.assertIsNot(result, vouchers)
        for normalized, raw in zip(result, vouchers):
            self.assertIsNot(normalized, raw)
            for normalized_entry, raw_entry in zip(normalized["entries"],
                                                   raw["entries"]):
                self.assertIsNot(normalized_entry, raw_entry)

    def test_huge_balanced_amounts_keep_full_integer_precision(self):
        # Float parsing would lose cents at this magnitude; integer-cents
        # arithmetic must reproduce the carry exactly.
        vouchers = [
            make_voucher(
                "V-BIG", "2024-01-01",
                entries=[
                    make_entry("6601", "大额一",
                               debit="99999999999999999999.99", credit="0.00"),
                    make_entry("6601", "尾差",
                               debit="0.01", credit="0.00"),
                    make_entry("2202", "对方科目",
                               debit="0.00",
                               credit="100000000000000000000.00"),
                ],
            )
        ]
        result = normalize_vouchers(BASE, make_chart(), vouchers)
        entries = result[0]["entries"]
        self.assertEqual(entries[0]["debit"], "99999999999999999999.99")
        self.assertEqual(entries[1]["debit"], "0.01")
        self.assertEqual(result[0]["debit_total"],
                         "100000000000000000000.00")
        self.assertEqual(result[0]["credit_total"],
                         "100000000000000000000.00")

    def test_fractional_addition_is_exact(self):
        # 0.10 + 0.20 must equal 0.30, never a binary-float artifact.
        vouchers = [
            make_voucher(
                "V-FRAC", "2024-01-01",
                entries=[
                    make_entry("6601", "一角", debit="0.10", credit="0.00"),
                    make_entry("6601", "两角", debit="0.20", credit="0.00"),
                    make_entry("2202", "三角", debit="0.00", credit="0.30"),
                ],
            )
        ]
        result = normalize_vouchers(BASE, make_chart(), vouchers)
        self.assertEqual(result[0]["debit_total"], "0.30")
        self.assertEqual(result[0]["credit_total"], "0.30")

    def test_zero_is_allowed_on_either_non_occurring_side(self):
        vouchers = [
            make_voucher(
                "V-ZERO-D", "2024-01-01",
                entries=[
                    make_entry("2202", "贷方发生", debit="0.00",
                               credit="7.00"),
                    make_entry("1001", "借方平衡", debit="7.00",
                               credit="0.00"),
                ],
            ),
            make_voucher(
                "V-ZERO-C", "2024-01-02",
                entries=[
                    make_entry("1001", "借方发生", debit="7",
                               credit="00"),
                    make_entry("2202", "贷方平衡", debit="0.0",
                               credit="7.0"),
                ],
            ),
        ]
        result = normalize_vouchers(BASE, make_chart(), vouchers)
        self.assertEqual(result[0]["debit_total"], "7.00")
        self.assertEqual(result[1]["credit_total"], "7.00")

    def test_valid_calendar_boundary_dates_accepted(self):
        for date_text in ("2024-02-29", "2000-02-29", "2023-02-28",
                          "2024-12-31", "2024-01-01"):
            with self.subTest(date_text=date_text):
                result = normalize_vouchers(
                    BASE, make_chart(), [make_voucher("V-D", date_text)])
                self.assertEqual(result[0]["date"], date_text)

    def test_valid_amount_boundary_strings_accepted(self):
        for value in ("1", "0.1", "0.01", "99", "99.9", "999999999999.99"):
            with self.subTest(value=value):
                vouchers = [
                    make_voucher(
                        "V-A", "2024-01-01",
                        entries=[
                            make_entry("6601", "借方", debit=value,
                                       credit="0.00"),
                            make_entry("2202", "贷方", debit="0.00",
                                       credit=value),
                        ],
                    )
                ]
                result = normalize_vouchers(BASE, make_chart(), vouchers)
                self.assertEqual(result[0]["debit_total"],
                                 result[0]["credit_total"])

    def test_valid_zero_forms_accepted_on_non_occurring_side(self):
        for zero in ("0", "00", "0.0", "0.00"):
            with self.subTest(zero=zero):
                vouchers = [
                    make_voucher(
                        "V-Z", "2024-01-01",
                        entries=[
                            make_entry("6601", "借方", debit="5.00",
                                       credit=zero),
                            make_entry("2202", "贷方", debit=zero,
                                       credit="5.00"),
                        ],
                    )
                ]
                result = normalize_vouchers(BASE, make_chart(), vouchers)
                self.assertEqual(result[0]["entries"][0]["credit"], "0.00")
                self.assertEqual(result[0]["entries"][1]["debit"], "0.00")


# --------------------------------------------------------------------- #
# Exception hierarchy
# --------------------------------------------------------------------- #
class ExceptionHierarchyTests(unittest.TestCase):
    def test_every_leaf_error_is_a_ledger_engine_error(self):
        for exc_class in LEAF_ERRORS:
            with self.subTest(exc_class=exc_class.__name__):
                self.assertTrue(issubclass(exc_class, LedgerEngineError))

    def test_leaf_classes_are_distinct(self):
        self.assertEqual(len(set(LEAF_ERRORS)), len(LEAF_ERRORS))

    def test_leaf_classes_are_not_subclasses_of_each_other(self):
        for exc_class in LEAF_ERRORS:
            others = [c for c in LEAF_ERRORS if c is not exc_class]
            for other in others:
                self.assertFalse(
                    issubclass(exc_class, other),
                    f"{exc_class.__name__} must not be a subclass of "
                    f"{other.__name__}",
                )


# --------------------------------------------------------------------- #
# ChartOfAccountsError
# --------------------------------------------------------------------- #
class ChartOfAccountsTests(NormalizeTestCase):
    VALID_VOUCHERS = [make_voucher()]

    def test_valid_chart_boundary_accepted(self):
        self.assert_normalizes(BASE, make_chart(),
                               copy.deepcopy(self.VALID_VOUCHERS),
                               [EXPECTED_BATCH[0]])

    def test_invalid_base_currencies_rejected(self):
        bad_values = ("cny", "CN", "CNYX", "CN1", " CNY", "CNY ", "CN ",
                      "", None, 123, True, False, ("CNY",))
        for value in bad_values:
            with self.subTest(value=value):
                self.assert_rejects(ChartOfAccountsError, value,
                                    make_chart(), make_valid_batch())

    def test_chart_must_be_a_list(self):
        for chart in (None, {}, tuple(make_chart()), make_chart()[0], "chart"):
            with self.subTest(chart=type(chart).__name__):
                self.assert_rejects(ChartOfAccountsError, BASE, chart,
                                    copy.deepcopy(self.VALID_VOUCHERS))

    def test_account_must_be_an_object(self):
        chart = ["not-an-object"]
        self.assert_rejects(ChartOfAccountsError, BASE, chart,
                            copy.deepcopy(self.VALID_VOUCHERS))

    def test_missing_account_fields_rejected(self):
        for field in ("code", "name", "normal_side", "active"):
            with self.subTest(field=field):
                account = {k: v for k, v in {
                    "code": "9999", "name": "新科目",
                    "normal_side": "debit", "active": True,
                }.items() if k != field}
                self.assert_rejects(ChartOfAccountsError, BASE, [account],
                                    copy.deepcopy(self.VALID_VOUCHERS))

    def test_bad_code_and_name_rejected(self):
        template = {"name": "科目", "normal_side": "debit", "active": True}
        for code in ("", 1001, None, True, ("1001",)):
            with self.subTest(code=code):
                account = dict(template, code=code)
                self.assert_rejects(ChartOfAccountsError, BASE, [account],
                                    copy.deepcopy(self.VALID_VOUCHERS))
        for name in ("", 0, None, True):
            with self.subTest(name=name):
                account = {"code": "9999", "name": name,
                           "normal_side": "debit", "active": True}
                self.assert_rejects(ChartOfAccountsError, BASE, [account],
                                    copy.deepcopy(self.VALID_VOUCHERS))

    def test_bad_normal_side_rejected(self):
        for side in ("Debit", "CREDIT", "credit ", " debit", "", None,
                     "debit\n", 0):
            with self.subTest(side=side):
                account = {"code": "9999", "name": "科目",
                           "normal_side": side, "active": True}
                self.assert_rejects(ChartOfAccountsError, BASE, [account],
                                    copy.deepcopy(self.VALID_VOUCHERS))

    def test_active_must_be_real_boolean(self):
        # 1/0 are the adjacent traps next to True/False and must not pass.
        for active in (1, 0, "true", "false", None, "yes", 2):
            with self.subTest(active=active):
                account = {"code": "9999", "name": "科目",
                           "normal_side": "debit", "active": active}
                self.assert_rejects(ChartOfAccountsError, BASE, [account],
                                    copy.deepcopy(self.VALID_VOUCHERS))

    def test_duplicate_account_code_rejected(self):
        chart = make_chart()
        chart.append({"code": "1001", "name": "重复现金",
                      "normal_side": "debit", "active": True})
        self.assert_rejects(ChartOfAccountsError, BASE, chart,
                            copy.deepcopy(self.VALID_VOUCHERS))

    def test_codes_differing_only_by_case_or_space_are_distinct(self):
        # No case folding, trimming or implicit normalization on chart codes.
        chart = [
            {"code": "1001", "name": "原科目", "normal_side": "debit",
             "active": True},
            {"code": "1001 ", "name": "带空格科目", "normal_side": "credit",
             "active": True},
            {"code": "1002", "name": "大写别名", "normal_side": "debit",
             "active": True},
        ]
        vouchers = [
            make_voucher(
                "V-EXACT", "2024-01-01",
                entries=[
                    make_entry("1001 ", "按带空格代码精确命中", debit="5.00"),
                    make_entry("1002", "平衡", debit="0.00", credit="5.00"),
                ],
            ),
        ]
        result = normalize_vouchers(BASE, chart, vouchers)
        entries = result[0]["entries"]
        self.assertEqual(entries[0]["account_code"], "1001 ")
        self.assertEqual(entries[0]["account_name"], "带空格科目")
        self.assertEqual(entries[0]["normal_side"], "credit")

    def test_chart_is_validated_before_vouchers(self):
        # Even a non-list vouchers payload must not mask a broken chart.
        self.assert_rejects(ChartOfAccountsError, BASE, ["bad"], None)
        self.assert_rejects(ChartOfAccountsError, "bad", make_chart(), None)


# --------------------------------------------------------------------- #
# VoucherFormatError
# --------------------------------------------------------------------- #
class VoucherFormatTests(NormalizeTestCase):
    def test_vouchers_must_be_a_list(self):
        for payload in (None, {}, tuple(), make_voucher(), "batch"):
            with self.subTest(payload=type(payload).__name__):
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    payload)

    def test_voucher_must_be_an_object(self):
        for payload in ("voucher", None, 42, ["list"], (make_voucher(),)):
            with self.subTest(payload=payload):
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [payload])

    def test_missing_voucher_fields_rejected(self):
        for field in ("voucher_id", "date", "currency", "entries"):
            with self.subTest(field=field):
                voucher = {k: v for k, v in make_voucher().items()
                           if k != field}
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [voucher])

    def test_voucher_id_must_be_non_empty_string(self):
        for voucher_id in (123, None, True, "", b"V-1", ("V-1",)):
            with self.subTest(voucher_id=voucher_id):
                voucher = make_voucher(voucher_id=voucher_id)
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [voucher])

    def test_invalid_calendar_dates_rejected(self):
        for date_text in ("2023-02-29", "2100-02-29", "1900-02-29",
                          "2024-02-30", "2024-04-31", "2024-13-01",
                          "2024-00-10", "2024-1-01", "2024/02/29",
                          "20240229", "2024-2-29", 20240229, None, True):
            with self.subTest(date_text=date_text):
                voucher = make_voucher(date_text=date_text)
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [voucher])

    def test_non_string_currency_is_format_error_not_currency_error(self):
        for currency in (123, None, True, ["CNY"]):
            with self.subTest(currency=currency):
                voucher = make_voucher(currency=currency)
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [voucher])

    def test_entries_must_be_list_of_at_least_two(self):
        # None is built literally here: make_voucher(entries=None) means
        # "use the default entries", per the factory contract.
        literal_none = {
            "voucher_id": "V-2024-001", "date": "2024-02-29",
            "currency": BASE, "entries": None,
        }
        for entries in ("entries", tuple(), [], [make_entry()]):
            with self.subTest(entries=entries):
                voucher = make_voucher(entries=entries)
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [voucher])
        self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                            [literal_none])

    def test_entry_must_be_an_object(self):
        voucher = make_voucher(entries=["entry", make_entry()])
        self.assert_rejects(VoucherFormatError, BASE, make_chart(), [voucher])

    def test_missing_entry_fields_rejected(self):
        for field in ("account_code", "summary", "debit", "credit"):
            with self.subTest(field=field):
                good = make_entry("6601", "x", debit="1.00")
                bad = {k: v for k, v in make_entry().items() if k != field}
                voucher = make_voucher(entries=[good, bad])
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [voucher])

    def test_account_code_must_be_string(self):
        for code in (1001, None, True, 10.01):
            with self.subTest(code=code):
                voucher = make_voucher(entries=[
                    make_entry(code, "x", debit="1.00"),
                    make_entry("2202", "y", credit="1.00"),
                ])
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [voucher])

    def test_summary_must_be_string(self):
        for summary in (None, 7, True):
            with self.subTest(summary=summary):
                voucher = make_voucher(entries=[
                    make_entry("6601", summary, debit="1.00"),
                    make_entry("2202", "x", credit="1.00"),
                ])
                self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                                    [voucher])

    def test_structural_checks_traverse_all_entries(self):
        # A defect in the *second* entry is found even though the voucher is
        # otherwise completely populated.
        voucher = make_voucher(entries=[
            make_entry("6601", "ok", debit="1.00"),
            make_entry("2202", None, credit="1.00"),
        ])
        self.assert_rejects(VoucherFormatError, BASE, make_chart(), [voucher])


# --------------------------------------------------------------------- #
# DuplicateVoucherError
# --------------------------------------------------------------------- #
class DuplicateVoucherTests(NormalizeTestCase):
    def test_exact_duplicate_voucher_id_rejected(self):
        vouchers = [make_voucher(), make_voucher()]  # same default id/date
        self.assert_rejects(DuplicateVoucherError, BASE, make_chart(),
                            vouchers)

    def test_duplicate_found_at_third_voucher(self):
        vouchers = [
            make_voucher("V-1", "2024-01-01"),
            make_voucher("V-2", "2024-01-02"),
            make_voucher("V-1", "2024-01-03"),
        ]
        self.assert_rejects(DuplicateVoucherError, BASE, make_chart(),
                            vouchers)

    def test_ids_differing_by_case_or_space_are_not_duplicates(self):
        vouchers = [
            make_voucher("V-001", "2024-01-01"),
            make_voucher("v-001", "2024-01-02"),
            make_voucher(" V-001", "2024-01-03"),
            make_voucher("V-001 ", "2024-01-04"),
        ]
        result = normalize_vouchers(BASE, make_chart(), vouchers)
        self.assertEqual([v["voucher_id"] for v in result],
                         ["V-001", "v-001", " V-001", "V-001 "])


# --------------------------------------------------------------------- #
# UnsupportedCurrencyError
# --------------------------------------------------------------------- #
class CurrencyTests(NormalizeTestCase):
    def test_foreign_and_near_miss_currencies_rejected(self):
        for currency in ("USD", "usd", "cny", "RMB", " CNY", "CNY ",
                         "CNY\n", "CN", "CNYY"):
            with self.subTest(currency=currency):
                voucher = make_voucher(currency=currency)
                self.assert_rejects(UnsupportedCurrencyError, BASE,
                                    make_chart(), [voucher])

    def test_exact_base_currency_boundary_accepted(self):
        result = normalize_vouchers(BASE, make_chart(), [make_voucher()])
        self.assertEqual(result[0]["currency"], "CNY")


# --------------------------------------------------------------------- #
# UnknownAccountError / InactiveAccountError
# --------------------------------------------------------------------- #
class AccountReferenceTests(NormalizeTestCase):
    def _balanced_with(self, first_code):
        return [make_voucher(entries=[
            make_entry(first_code, "借方", debit="1.00"),
            make_entry("2202", "贷方", credit="1.00"),
        ])]

    def test_unknown_account_codes_rejected(self):
        for code in ("9999", "1001 ", " 1001", "1001\n", "100A", "unknown",
                     ""):
            with self.subTest(code=code):
                self.assert_rejects(UnknownAccountError, BASE, make_chart(),
                                    self._balanced_with(code))

    def test_code_case_is_significant(self):
        # "1001" exists; a different literal must not resolve to it.
        self.assert_rejects(UnknownAccountError, BASE, make_chart(),
                            self._balanced_with("1001x"))

    def test_inactive_account_rejected(self):
        self.assert_rejects(InactiveAccountError, BASE, make_chart(),
                            self._balanced_with("4001"))

    def test_active_flag_boundary_flips_result(self):
        chart = make_chart()
        chart[3]["active"] = True
        result = normalize_vouchers(BASE, chart,
                                    self._balanced_with("4001"))
        self.assertEqual(result[0]["entries"][0]["account_code"], "4001")


# --------------------------------------------------------------------- #
# InvalidEntryAmountError
# --------------------------------------------------------------------- #
class EntryAmountTests(NormalizeTestCase):
    BAD_AMOUNTS = (
        "-1", "-0.01", "+1", "1e3", "1E3", "1,000.00", "1,00",
        " 100", "100 ", "\t100", "100\n", " 100 ", ".5", "1.",
        "1.234", "0.001", "0.0001", "", "  ", "nan", "NaN", "Inf",
        "0x10", "1_000", "00.1a", "１", "①", "1.2.3",
    )
    BAD_NON_STRING_AMOUNTS = (0, 1, 100, 0.0, 1.5, True, False, None,
                              [100], {"value": 100})

    def _voucher_with_amount(self, debit, credit):
        return [make_voucher(entries=[
            make_entry("6601", "借方", debit=debit, credit="0.00"),
            make_entry("2202", "贷方", debit="0.00", credit="1.00"),
        ])]

    def test_loosely_numeric_strings_rejected_on_debit_side(self):
        for value in self.BAD_AMOUNTS:
            with self.subTest(value=value):
                self.assert_rejects(InvalidEntryAmountError, BASE,
                                    make_chart(),
                                    self._voucher_with_amount(value, "0.00"))

    def test_loosely_numeric_strings_rejected_on_credit_side(self):
        for value in self.BAD_AMOUNTS:
            with self.subTest(value=value):
                voucher = [make_voucher(entries=[
                    make_entry("6601", "借方", debit="1.00", credit="0.00"),
                    make_entry("2202", "贷方", debit="0.00", credit=value),
                ])]
                self.assert_rejects(InvalidEntryAmountError, BASE,
                                    make_chart(), voucher)

    def test_numeric_and_boolean_amounts_rejected(self):
        for value in self.BAD_NON_STRING_AMOUNTS:
            with self.subTest(value=value):
                self.assert_rejects(InvalidEntryAmountError, BASE,
                                    make_chart(),
                                    self._voucher_with_amount(value, "0.00"))

    def test_both_sides_zero_rejected(self):
        for debit, credit in (("0", "0"), ("00", "0.00"),
                              ("0.0", "0.00"), ("0.00", "000.00")):
            with self.subTest(debit=debit, credit=credit):
                # First entry has no occurring side; the second keeps the
                # voucher balanced so the entry defect is what is reported.
                voucher = [make_voucher(entries=[
                    make_entry("6601", "双零", debit=debit, credit=credit),
                    make_entry("2202", "平衡", debit="1.00", credit="0.00"),
                ])]
                self.assert_rejects(InvalidEntryAmountError, BASE,
                                    make_chart(), voucher)

    def test_both_sides_nonzero_rejected(self):
        voucher = [make_voucher(entries=[
            make_entry("6601", "双边", debit="1.00", credit="1.00"),
            make_entry("2202", "占位", debit="0.00", credit="0.00"),
        ])]
        # Line 1 violates exactly-one-side first; line 2's both-zero defect
        # must never be the reported one.
        self.assert_rejects(InvalidEntryAmountError, BASE, make_chart(),
                            voucher)

    def test_exactly_one_side_boundary_accepted(self):
        cases = [
            (["0.01", "0.00"], ["0.00", "0.01"]),   # debit then credit
            (["0.00", "0.01"], ["0.01", "0.00"]),   # credit then debit
            (["99999999999999999999.99", "0"],
             ["0", "99999999999999999999.99"]),     # huge, terse zero
        ]
        for first_sides, second_sides in cases:
            with self.subTest(first=first_sides):
                voucher = [make_voucher(entries=[
                    make_entry("6601", "一边",
                               debit=first_sides[0], credit=first_sides[1]),
                    make_entry("2202", "对边",
                               debit=second_sides[0], credit=second_sides[1]),
                ])]
                result = normalize_vouchers(BASE, make_chart(), voucher)
                self.assertEqual(result[0]["debit_total"],
                                 result[0]["credit_total"])


# --------------------------------------------------------------------- #
# UnbalancedVoucherError
# --------------------------------------------------------------------- #
class BalanceTests(NormalizeTestCase):
    def test_unequal_totals_rejected(self):
        voucher = [make_voucher(entries=[
            make_entry("6601", "借方", debit="100.00"),
            make_entry("2202", "贷方", credit="99.99"),
        ])]
        self.assert_rejects(UnbalancedVoucherError, BASE, make_chart(),
                            voucher)

    def test_fractional_unequal_totals_rejected(self):
        voucher = [make_voucher(entries=[
            make_entry("6601", "一角", debit="0.10"),
            make_entry("6601", "两角", debit="0.20"),
            make_entry("2202", "短一分", credit="0.29"),
        ])]
        self.assert_rejects(UnbalancedVoucherError, BASE, make_chart(),
                            voucher)

    def test_equal_totals_boundary_accepted(self):
        voucher = [make_voucher(entries=[
            make_entry("6601", "一角", debit="0.10"),
            make_entry("6601", "两角", debit="0.20"),
            make_entry("2202", "三角", credit="0.30"),
        ])]
        result = normalize_vouchers(BASE, make_chart(), voucher)
        self.assertEqual(result[0]["debit_total"], "0.30")


# --------------------------------------------------------------------- #
# Validation order: the first problem wins, later problems never mask it.
# --------------------------------------------------------------------- #
class ValidationOrderTests(NormalizeTestCase):
    def test_chart_problem_masks_voucher_problem(self):
        self.assert_rejects(ChartOfAccountsError, BASE, ["broken"],
                            [make_voucher()])

    def test_earlier_voucher_failure_masks_later_duplicate(self):
        bad_first = make_voucher(entries=[make_entry()])  # only one entry
        vouchers = [bad_first, make_voucher()]  # second would be duplicate
        self.assert_rejects(VoucherFormatError, BASE, make_chart(), vouchers)

    def test_structural_defect_masks_duplicate_id(self):
        duplicate_but_malformed = make_voucher(
            entries=[make_entry("6601", "only one")])
        vouchers = [make_voucher(), duplicate_but_malformed]
        self.assert_rejects(VoucherFormatError, BASE, make_chart(), vouchers)

    def test_structural_defect_masks_foreign_currency(self):
        voucher = make_voucher(currency="USD", date_text="2023-02-29")
        self.assert_rejects(VoucherFormatError, BASE, make_chart(), [voucher])

    def test_duplicate_id_masks_foreign_currency(self):
        duplicate_foreign = make_voucher(currency="USD")
        vouchers = [make_voucher(), duplicate_foreign]
        self.assert_rejects(DuplicateVoucherError, BASE, make_chart(),
                            vouchers)

    def test_foreign_currency_masks_unknown_account(self):
        voucher = make_voucher(currency="USD", entries=[
            make_entry("9999", "未知", debit="1.00"),
            make_entry("2202", "贷方", credit="1.00"),
        ])
        self.assert_rejects(UnsupportedCurrencyError, BASE, make_chart(),
                            [voucher])

    def test_unknown_account_masks_inactive_and_amount_defects(self):
        voucher = make_voucher(entries=[
            make_entry("8888", "未知且金额坏", debit="bad", credit="also"),
            make_entry("4001", "停用", credit="1.00"),
        ])
        self.assert_rejects(UnknownAccountError, BASE, make_chart(), [voucher])

    def test_inactive_account_is_reported_in_entry_order(self):
        # Inactive first entry beats the unknown code on the second entry.
        voucher = make_voucher(entries=[
            make_entry("4001", "停用", debit="1.00"),
            make_entry("8888", "未知", credit="1.00"),
        ])
        self.assert_rejects(InactiveAccountError, BASE, make_chart(),
                            [voucher])

    def test_unknown_on_first_entry_beats_inactive_on_second(self):
        voucher = make_voucher(entries=[
            make_entry("8888", "未知", debit="1.00"),
            make_entry("4001", "停用", credit="1.00"),
        ])
        self.assert_rejects(UnknownAccountError, BASE, make_chart(),
                            [voucher])

    def test_inactive_account_masks_bad_amount(self):
        voucher = make_voucher(entries=[
            make_entry("4001", "停用且金额坏", debit="bad"),
            make_entry("2202", "贷方", credit="1.00"),
        ])
        self.assert_rejects(InactiveAccountError, BASE, make_chart(),
                            [voucher])

    def test_non_string_account_code_masks_unknown_lookup(self):
        # 1001 exists as a string; the int 1001 is a format defect, never an
        # implicit conversion that would resolve to the string account.
        voucher = make_voucher(entries=[
            make_entry(1001, "数字代码", debit="1.00"),
            make_entry("2202", "贷方", credit="1.00"),
        ])
        self.assert_rejects(VoucherFormatError, BASE, make_chart(), [voucher])

    def test_non_string_voucher_id_masks_duplicate_detection(self):
        voucher = make_voucher(voucher_id=1001)
        self.assert_rejects(VoucherFormatError, BASE, make_chart(), [voucher])

    def test_bad_amount_masks_unbalanced_totals(self):
        voucher = make_voucher(entries=[
            make_entry("6601", "金额坏", debit="1,000.00"),
            make_entry("2202", "贷方", credit="999.00"),
        ])
        self.assert_rejects(InvalidEntryAmountError, BASE, make_chart(),
                            [voucher])

    def test_balance_is_checked_last(self):
        # Every earlier phase passes; only the totals differ.
        voucher = make_voucher(entries=[
            make_entry("6601", "借方", debit="1.00"),
            make_entry("6601", "再借", debit="2.50"),
            make_entry("2202", "贷方", credit="3.00"),
        ])
        self.assert_rejects(UnbalancedVoucherError, BASE, make_chart(),
                            [voucher])

    def test_validation_stops_at_first_voucher(self):
        vouchers = [
            make_voucher("V-OK", "2024-01-01"),
            make_voucher("V-BAD", "2024-01-02", currency="USD"),
            make_voucher("V-OK", "2024-01-03"),  # duplicate of first
        ]
        self.assert_rejects(UnsupportedCurrencyError, BASE, make_chart(),
                            vouchers)


# --------------------------------------------------------------------- #
# Loose-parsing traps consolidated
# --------------------------------------------------------------------- #
class StrictParsingTests(NormalizeTestCase):
    def test_negative_exponent_thousands_whitespace_leading_dot_rejected(self):
        for value in ("-100", "1e2", "1,000", " 100", "100 ", ".5",
                      "100.", "+100"):
            with self.subTest(value=value):
                voucher = [make_voucher(entries=[
                    make_entry("6601", "陷阱", debit=value),
                    make_entry("2202", "贷方", credit="1.00"),
                ])]
                self.assert_rejects(InvalidEntryAmountError, BASE,
                                    make_chart(), voucher)

    def test_lowercase_currency_not_silently_accepted(self):
        voucher = [make_voucher(currency="cny")]
        self.assert_rejects(UnsupportedCurrencyError, BASE, make_chart(),
                            voucher)

    def test_invalid_leap_day_rejected_but_valid_leap_day_accepted(self):
        self.assert_rejects(VoucherFormatError, BASE, make_chart(),
                            [make_voucher(date_text="2023-02-29")])
        result = normalize_vouchers(
            BASE, make_chart(), [make_voucher(date_text="2024-02-29")])
        self.assertEqual(result[0]["date"], "2024-02-29")

    def test_boolean_amounts_rejected(self):
        for value in (True, False):
            with self.subTest(value=value):
                voucher = [make_voucher(entries=[
                    make_entry("6601", "布尔", debit=value),
                    make_entry("2202", "贷方", credit="1.00"),
                ])]
                self.assert_rejects(InvalidEntryAmountError, BASE,
                                    make_chart(), voucher)

    def test_numeric_amounts_rejected_even_if_values_equal(self):
        voucher = [make_voucher(entries=[
            make_entry("6601", "整数", debit=100, credit=0),
            make_entry("2202", "浮点", debit=0, credit=100.0),
        ])]
        self.assert_rejects(InvalidEntryAmountError, BASE, make_chart(),
                            voucher)

    def test_account_code_and_voucher_id_compared_as_raw_strings(self):
        # Trailing/leading spaces and case change route nowhere / never match.
        self.assert_rejects(UnknownAccountError, BASE, make_chart(),
                            [make_voucher(entries=[
                                make_entry(" 1001", "前导空格", debit="1.00"),
                                make_entry("2202", "贷方", credit="1.00"),
                            ])])
        result = normalize_vouchers(BASE, make_chart(), [
            make_voucher("Vx", "2024-01-01"),
            make_voucher("VX", "2024-01-02"),
        ])
        self.assertEqual([v["voucher_id"] for v in result], ["Vx", "VX"])


# --------------------------------------------------------------------- #
# CLI surface: version/help stay as released
# --------------------------------------------------------------------- #
class CliSurfaceTests(unittest.TestCase):
    def test_version_is_unchanged(self):
        self.assertEqual(__version__, "0.1.0")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["version"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "0.1.0")

    def test_help_lists_both_commands(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["help"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("version", text)
        self.assertIn("help", text)

    def test_unknown_command_returns_two(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown command", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
