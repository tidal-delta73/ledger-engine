"""Public-contract tests for recomputation consistency and input immutability.

The baseline already exposes two public entry points --
``normalize_vouchers`` and ``post_vouchers`` -- and guarantees integer-cents
arithmetic, fixed field order and batch-wide validate-before-return.  This
module adds no business interface; it pins, through the public surface
only, the properties a repeated recomputation must preserve:

* repeated processing of the same chart/batch returns equal values while
  reusing no mutable container at any nesting level;
* deeply mutating one result never changes a later result, which stays
  identical to an unmodified snapshot of the first;
* inputs that are equal but built from different container instances or
  dict insertion orders produce value-equal and, with fixed JSON keyword
  arguments, character-for-character identical documents;
* the raw chart and voucher batch stay layer-wise equal after successful
  calls and after failed ones;
* failure handling keeps its deterministic first-boundary behaviour and
  both entry points raise the same first leaf exception for one input.

Nothing private is imported: only the package entry points, the published
exception hierarchy and the released CLI ``main`` are used.  Assertions
never pin full exception wording (only equality of the message between
the two entry points), dict/set hash iteration order, or third-party
packages.  Standard library only; CPython 3.10+.
"""
from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
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
    __all__ as PUBLIC_NAMES,
    __version__,
    normalize_vouchers,
    post_vouchers,
)
from ledger_engine.__main__ import main

BASE = "CNY"

# 2**53 + 1 expressed in whole yuan: a plain binary float cannot represent
# this integer exactly, so float handling would corrupt the cents arithmetic.
BIG = "9007199254740993.00"

# Pinned serialization parameters: the text contract cannot drift with
# environment defaults (key order, ASCII escaping, separators).
JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}
COMPACT_KW = {"ensure_ascii": False, "separators": (",", ":")}

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


# --------------------------------------------------------------------- #
# Dataset, rendered in two semantically equal layouts.
#
# The chart deliberately contains an unused active account (5001), an
# unused inactive account (4001), and accounts with both normal sides.
# The batch contains several vouchers, terse amount spellings ("00",
# "0.0", "0"), a zero-net account (2202), a reverse-net account
# (1001 is debit-normal but ends credit-side) and a beyond-float-safe
# amount (BIG).
# --------------------------------------------------------------------- #
_CHART_ROWS = [
    ("1001", "库存现金", "debit", True),
    ("2202", "应付账款", "credit", True),
    ("6601", "管理费用", "debit", True),
    ("5001", "主营业务收入", "credit", True),
    ("4001", "停用收入", "credit", False),
]

# (voucher_id, date, currency, [(account_code, summary, debit, credit)])
_VOUCHER_SPECS = [
    ("V-2024-001", "2024-01-15", BASE, [
        ("6601", "报销费用", "1234.56", "00"),
        ("2202", "应付入账", "0.0", "1234.56"),
    ]),
    ("V-2024-002", "2024-02-20", BASE, [
        ("6601", "费用冲回", "0", "600.00"),
        ("1001", "现金支付", "0.00", "634.56"),
        ("2202", "偿还欠款", "1234.56", "0"),
    ]),
    ("V-BIG", "2024-03-05", BASE, [
        ("6601", "大额费用", BIG, "0.00"),
        ("2202", "大额应付", "00", BIG),
    ]),
]


def make_entry(account_code="1001", summary="业务", debit="0.00",
               credit="0.00"):
    return {
        "account_code": account_code,
        "summary": summary,
        "debit": debit,
        "credit": credit,
    }


def make_voucher(voucher_id="V-2024-001", date_text="2024-01-15",
                 currency=BASE, entries=None):
    if entries is None:
        entries = [
            make_entry("6601", "报销费用", debit="1234.56", credit="00"),
            make_entry("2202", "应付入账", debit="0.0", credit="1234.56"),
        ]
    return {
        "voucher_id": voucher_id,
        "date": date_text,
        "currency": currency,
        "entries": entries,
    }


def make_chart():
    return [
        {"code": code, "name": name, "normal_side": side, "active": active}
        for code, name, side, active in _CHART_ROWS
    ]


def make_batch():
    batch = []
    for voucher_id, date_text, currency, entry_specs in _VOUCHER_SPECS:
        batch.append(make_voucher(
            voucher_id, date_text, currency,
            [make_entry(*spec) for spec in entry_specs]))
    return batch


# Equivalent layout: every dict is built with a deliberately different
# key insertion order.  ``==`` treats these as equal to the standard
# layout, but a result builder that followed raw insertion order would
# leak this ordering into its output documents.
def make_chart_shuffled():
    return [
        {"active": active, "normal_side": side, "name": name, "code": code}
        for code, name, side, active in _CHART_ROWS
    ]


def _shuffled_entry(account_code, summary, debit, credit):
    return {"credit": credit, "debit": debit, "summary": summary,
            "account_code": account_code}


def _shuffled_voucher(voucher_id, date_text, currency, entries):
    return {"entries": entries, "currency": currency, "date": date_text,
            "voucher_id": voucher_id}


def make_batch_shuffled():
    batch = []
    for voucher_id, date_text, currency, entry_specs in _VOUCHER_SPECS:
        batch.append(_shuffled_voucher(
            voucher_id, date_text, currency,
            [_shuffled_entry(*spec) for spec in entry_specs]))
    return batch


# Full value contract for normalization of the dataset.
EXPECTED_NORMALIZED = [
    {
        "voucher_id": "V-2024-001",
        "date": "2024-01-15",
        "currency": "CNY",
        "entries": [
            {"line_no": 1, "account_code": "6601", "account_name": "管理费用",
             "normal_side": "debit", "summary": "报销费用",
             "debit": "1234.56", "credit": "0.00"},
            {"line_no": 2, "account_code": "2202", "account_name": "应付账款",
             "normal_side": "credit", "summary": "应付入账",
             "debit": "0.00", "credit": "1234.56"},
        ],
        "debit_total": "1234.56",
        "credit_total": "1234.56",
    },
    {
        "voucher_id": "V-2024-002",
        "date": "2024-02-20",
        "currency": "CNY",
        "entries": [
            {"line_no": 1, "account_code": "6601", "account_name": "管理费用",
             "normal_side": "debit", "summary": "费用冲回",
             "debit": "0.00", "credit": "600.00"},
            {"line_no": 2, "account_code": "1001", "account_name": "库存现金",
             "normal_side": "debit", "summary": "现金支付",
             "debit": "0.00", "credit": "634.56"},
            {"line_no": 3, "account_code": "2202", "account_name": "应付账款",
             "normal_side": "credit", "summary": "偿还欠款",
             "debit": "1234.56", "credit": "0.00"},
        ],
        "debit_total": "1234.56",
        "credit_total": "1234.56",
    },
    {
        "voucher_id": "V-BIG",
        "date": "2024-03-05",
        "currency": "CNY",
        "entries": [
            {"line_no": 1, "account_code": "6601", "account_name": "管理费用",
             "normal_side": "debit", "summary": "大额费用",
             "debit": BIG, "credit": "0.00"},
            {"line_no": 2, "account_code": "2202", "account_name": "应付账款",
             "normal_side": "credit", "summary": "大额应付",
             "debit": "0.00", "credit": BIG},
        ],
        "debit_total": BIG,
        "credit_total": BIG,
    },
]

# Byte-level fragments pin the fixed insertion orders even without
# sort_keys, including the first journal row and the reversed-side and
# zero-side account summaries.
GOLDEN_FIRST_VOUCHER = (
    '{"voucher_id":"V-2024-001","date":"2024-01-15","currency":"CNY",'
    '"entries":[{"line_no":1,"account_code":"6601","account_name":"管理费用",'
    '"normal_side":"debit","summary":"报销费用","debit":"1234.56",'
    '"credit":"0.00"},{"line_no":2,"account_code":"2202",'
    '"account_name":"应付账款","normal_side":"credit","summary":"应付入账",'
    '"debit":"0.00","credit":"1234.56"}],"debit_total":"1234.56",'
    '"credit_total":"1234.56"}'
)
GOLDEN_FIRST_JOURNAL_ROW = (
    '{"voucher_id":"V-2024-001","date":"2024-01-15","line_no":1,'
    '"account_code":"6601","account_name":"管理费用","summary":"报销费用",'
    '"debit":"1234.56","credit":"0.00"}'
)
GOLDEN_CASH_ACCOUNT_ROW = (
    '{"code":"1001","name":"库存现金","normal_side":"debit",'
    '"debit_turnover":"0.00","credit_turnover":"634.56",'
    '"ending_side":"credit","ending_balance":"634.56"}'
)
GOLDEN_PAYABLES_ACCOUNT_ROW = (
    '{"code":"2202","name":"应付账款","normal_side":"credit",'
    '"debit_turnover":"1234.56","credit_turnover":"9007199254742227.56",'
    '"ending_side":"credit","ending_balance":"9007199254740993.00"}'
)


# --------------------------------------------------------------------- #
# Container walking helpers: only mutable JSON containers are considered,
# never strings/ints (interning would make identity comparisons of values
# meaningless).
# --------------------------------------------------------------------- #
def _walk_containers(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_containers(value)
    elif isinstance(obj, list):
        yield obj
        for value in obj:
            yield from _walk_containers(value)


_MUTATION_MARKER = "__mutation_marker__"


def _damage_every_container(obj):
    """Deeply mutate every list/dict in place; children first so the
    marker itself is never recursed into."""
    if isinstance(obj, dict):
        for value in list(obj.values()):
            _damage_every_container(value)
        obj[_MUTATION_MARKER] = _MUTATION_MARKER
    elif isinstance(obj, list):
        for value in list(obj):
            _damage_every_container(value)
        obj.append(_MUTATION_MARKER)


class PublicContractTestCase(unittest.TestCase):
    def assert_container_ids_disjoint(self, left, right):
        left_ids = {id(c) for c in _walk_containers(left)}
        right_ids = {id(c) for c in _walk_containers(right)}
        shared = left_ids & right_ids
        self.assertFalse(
            shared,
            f"results reuse mutable container(s): {len(shared)} id(s) shared",
        )

    def assert_recomputes_unchanged(self, entry_point, chart, batch):
        """Run one entry point three times over the same live inputs.

        Values must stay equal and no pair of results may share a list or
        dict at any nesting level.
        """
        results = [entry_point(BASE, chart, batch) for _ in range(3)]
        for result in results[1:]:
            self.assertEqual(result, results[0])
        self.assert_container_ids_disjoint(results[0], results[1])
        self.assert_container_ids_disjoint(results[0], results[2])
        self.assert_container_ids_disjoint(results[1], results[2])
        return results

    def assert_leaf_failure_both_entry_points(self, exc_type, base, chart,
                                             vouchers):
        """Both public entries must raise the exact leaf class with the
        same message, leave inputs layer-wise equal, and return nothing.
        """
        message = None
        for entry_point in (normalize_vouchers, post_vouchers):
            snapshot = copy.deepcopy((base, chart, vouchers))
            with self.assertRaises(exc_type) as caught:
                entry_point(base, chart, vouchers)
            self.assertIs(type(caught.exception), exc_type,
                          "reported category must be the leaf class itself")
            self.assertEqual((base, chart, vouchers), snapshot,
                             "failed call must leave every input equal")
            if message is None:
                message = str(caught.exception)
            else:
                # Same shared pipeline -> same first-boundary message,
                # without this test pinning that wording itself.
                self.assertEqual(str(caught.exception), message)
        return message


# --------------------------------------------------------------------- #
# Repeated recomputation: equality and container isolation
# --------------------------------------------------------------------- #
class RecomputationTests(PublicContractTestCase):
    def test_normalize_repeated_calls_are_equal_and_fresh_at_every_level(self):
        results = self.assert_recomputes_unchanged(
            normalize_vouchers, make_chart(), make_batch())
        self.assertEqual(results[0], EXPECTED_NORMALIZED)

    def test_post_repeated_calls_are_equal_and_fresh_at_every_level(self):
        results = self.assert_recomputes_unchanged(
            post_vouchers, make_chart(), make_batch())
        self.assertEqual(list(results[0]),
                         ["base_currency", "journal", "accounts"])
        self.assertEqual(len(results[0]["journal"]), 7)
        self.assertEqual(len(results[0]["accounts"]), 5)

    def test_normalize_result_shares_no_container_with_raw_inputs(self):
        chart = make_chart()
        batch = make_batch()
        result = normalize_vouchers(BASE, chart, batch)
        raw = [chart, batch]
        self.assert_container_ids_disjoint(result, raw)

    def test_post_result_shares_no_container_with_raw_inputs(self):
        chart = make_chart()
        batch = make_batch()
        result = post_vouchers(BASE, chart, batch)
        self.assert_container_ids_disjoint(result, [chart, batch])

    def test_normalize_survives_deep_mutation_of_a_prior_result(self):
        chart, batch = make_chart(), make_batch()
        first = normalize_vouchers(BASE, chart, batch)
        pristine_snapshot = copy.deepcopy(first)
        _damage_every_container(first)
        self.assertNotEqual(first, pristine_snapshot)  # damage really landed
        second = normalize_vouchers(BASE, chart, batch)
        third = normalize_vouchers(BASE, make_chart(), make_batch())
        self.assertEqual(second, pristine_snapshot)
        self.assertEqual(third, pristine_snapshot)
        self.assertEqual(
            json.dumps(second, **JSON_KW),
            json.dumps(pristine_snapshot, **JSON_KW),
        )

    def test_post_survives_deep_mutation_of_a_prior_result(self):
        chart, batch = make_chart(), make_batch()
        first = post_vouchers(BASE, chart, batch)
        pristine_snapshot = copy.deepcopy(first)
        _damage_every_container(first)
        journal = first["journal"]  # damaged: a marker was appended
        self.assertTrue(any(item == _MUTATION_MARKER for item in journal))
        second = post_vouchers(BASE, chart, batch)
        third = post_vouchers(BASE, make_chart(), make_batch())
        self.assertEqual(second, pristine_snapshot)
        self.assertEqual(third, pristine_snapshot)
        self.assertEqual(
            json.dumps(second, **JSON_KW),
            json.dumps(pristine_snapshot, **JSON_KW),
        )

    def test_failed_call_leaves_no_partial_state_for_later_success(self):
        chart = make_chart()
        good = make_voucher()
        duplicate_batch = [good, copy.deepcopy(good)]
        for entry_point in (normalize_vouchers, post_vouchers):
            with self.assertRaises(DuplicateVoucherError):
                entry_point(BASE, chart, duplicate_batch)
        # Correcting the batch yields the complete result, not a partial
        # replay of anything accumulated before the failure.
        normalized = normalize_vouchers(BASE, chart, [good])
        posted = post_vouchers(BASE, chart, [good])
        self.assertEqual(len(normalized), 1)
        self.assertEqual(len(normalized[0]["entries"]), 2)
        self.assertEqual(len(posted["journal"]), 2)
        self.assertEqual(len(posted["accounts"]), 5)


# --------------------------------------------------------------------- #
# Raw inputs stay untouched, layer by layer, after success and failure
# --------------------------------------------------------------------- #
class InputImmutabilityTests(PublicContractTestCase):
    def test_inputs_unchanged_through_interleaved_success_calls(self):
        chart, batch = make_chart(), make_batch()
        snapshot = copy.deepcopy((BASE, chart, batch))
        normalize_vouchers(BASE, chart, batch)
        post_vouchers(BASE, chart, batch)
        normalize_vouchers(BASE, chart, batch)
        self.assertEqual((BASE, chart, batch), snapshot)
        # Terse raw spellings survive verbatim; output-only fields are
        # never written back into the caller's dicts.
        self.assertEqual(batch[0]["entries"][0]["credit"], "00")
        self.assertEqual(batch[0]["entries"][1]["debit"], "0.0")
        self.assertEqual(batch[1]["entries"][0]["debit"], "0")
        self.assertNotIn("line_no", batch[0]["entries"][0])
        self.assertNotIn("debit_total", batch[0])
        self.assertEqual(
            set(chart[0]), {"code", "name", "normal_side", "active"})

    def test_inputs_unchanged_after_every_failure_boundary(self):
        for exc_type, (base, chart, vouchers) in FAILURE_CASES:
            with self.subTest(exc=exc_type.__name__):
                # The shared assertion helper deep-copies a snapshot around
                # each of the two public calls and verifies layer-wise
                # equality afterwards.
                self.assert_leaf_failure_both_entry_points(
                    exc_type, base, chart, vouchers)

    def test_chart_mutation_after_a_call_cannot_reach_earlier_results(self):
        chart = make_chart()
        first_n = normalize_vouchers(BASE, chart, make_batch())
        first_p = post_vouchers(BASE, chart, make_batch())
        snapshot_n = copy.deepcopy(first_n)
        snapshot_p = copy.deepcopy(first_p)
        chart[0]["name"] = "被篡改的科目名"
        chart[0]["active"] = False
        chart.append({"code": "9999", "name": "事后新增",
                      "normal_side": "debit", "active": True})
        self.assertEqual(first_n, snapshot_n)
        self.assertEqual(first_p, snapshot_p)


# --------------------------------------------------------------------- #
# Equivalent inputs (new container instances, shuffled dict insertion
# order) must give equal and byte-identical serialized documents.
# --------------------------------------------------------------------- #
class InsertionOrderIndependenceTests(PublicContractTestCase):
    def setUp(self):
        self.chart_a, self.batch_a = make_chart(), make_batch()
        self.chart_b, self.batch_b = (make_chart_shuffled(),
                                      make_batch_shuffled())

    def test_shuffled_layouts_are_equal_inputs_but_distinct_objects(self):
        self.assertEqual(self.chart_a, self.chart_b)
        self.assertEqual(self.batch_a, self.batch_b)
        self.assertIsNot(self.chart_a, self.chart_b)
        self.assertIsNot(self.batch_a, self.batch_b)
        self.assertIsNot(self.batch_a[0], self.batch_b[0])
        # Raw insertion orders really do differ; the outputs must not care.
        self.assertEqual(list(self.chart_a[0]),
                         ["code", "name", "normal_side", "active"])
        self.assertEqual(list(self.chart_b[0]),
                         ["active", "normal_side", "name", "code"])
        self.assertEqual(list(self.batch_a[0]),
                         ["voucher_id", "date", "currency", "entries"])
        self.assertEqual(list(self.batch_b[0]),
                         ["entries", "currency", "date", "voucher_id"])

    def test_normalize_shuffled_layouts_produce_equal_results(self):
        result_a = normalize_vouchers(BASE, self.chart_a, self.batch_a)
        result_b = normalize_vouchers(BASE, self.chart_b, self.batch_b)
        self.assertEqual(result_a, result_b)
        self.assertEqual(result_a, EXPECTED_NORMALIZED)
        self.assert_container_ids_disjoint(result_a, result_b)

    def test_post_shuffled_layouts_produce_equal_results(self):
        result_a = post_vouchers(BASE, self.chart_a, self.batch_a)
        result_b = post_vouchers(BASE, self.chart_b, self.batch_b)
        self.assertEqual(result_a, result_b)
        self.assert_container_ids_disjoint(result_a, result_b)

    def test_fixed_json_normalize_is_character_identical(self):
        text_a = json.dumps(
            normalize_vouchers(BASE, self.chart_a, self.batch_a), **JSON_KW)
        text_b = json.dumps(
            normalize_vouchers(BASE, self.chart_b, self.batch_b), **JSON_KW)
        text_c = json.dumps(
            normalize_vouchers(BASE, make_chart(), make_batch()), **JSON_KW)
        self.assertEqual(text_a, text_b)
        self.assertEqual(text_a, text_c)
        self.assertEqual(text_a.encode("utf-8"), text_b.encode("utf-8"))

    def test_fixed_json_post_is_character_identical(self):
        text_a = json.dumps(
            post_vouchers(BASE, self.chart_a, self.batch_a), **JSON_KW)
        text_b = json.dumps(
            post_vouchers(BASE, self.chart_b, self.batch_b), **JSON_KW)
        self.assertEqual(text_a, text_b)
        self.assertEqual(text_a.encode("utf-8"), text_b.encode("utf-8"))

    def test_result_key_order_is_fixed_without_sort_keys(self):
        normalized = normalize_vouchers(BASE, self.chart_b, self.batch_b)
        posted = post_vouchers(BASE, self.chart_b, self.batch_b)
        self.assertEqual(
            list(normalized[0]),
            ["voucher_id", "date", "currency", "entries",
             "debit_total", "credit_total"],
        )
        self.assertEqual(
            list(normalized[0]["entries"][0]),
            ["line_no", "account_code", "account_name", "normal_side",
             "summary", "debit", "credit"],
        )
        self.assertEqual(
            list(posted["journal"][0]),
            ["voucher_id", "date", "line_no", "account_code",
             "account_name", "summary", "debit", "credit"],
        )
        self.assertEqual(
            list(posted["accounts"][0]),
            ["code", "name", "normal_side", "debit_turnover",
             "credit_turnover", "ending_side", "ending_balance"],
        )
        self.assertEqual(
            json.dumps(normalized[0], **COMPACT_KW), GOLDEN_FIRST_VOUCHER)
        self.assertEqual(
            json.dumps(posted["journal"][0], **COMPACT_KW),
            GOLDEN_FIRST_JOURNAL_ROW)
        self.assertEqual(
            json.dumps(posted["accounts"][0], **COMPACT_KW),
            GOLDEN_CASH_ACCOUNT_ROW)
        self.assertEqual(
            json.dumps(posted["accounts"][1], **COMPACT_KW),
            GOLDEN_PAYABLES_ACCOUNT_ROW)


# --------------------------------------------------------------------- #
# Success scenario: ordering, two-place rendering, journal expansion,
# account summaries with zero/reversed nets and exact huge arithmetic.
# --------------------------------------------------------------------- #
class SuccessScenarioTests(unittest.TestCase):
    def setUp(self):
        self.normalized = normalize_vouchers(BASE, make_chart(), make_batch())
        self.posted = post_vouchers(BASE, make_chart(), make_batch())

    def test_normalized_values_match_full_contract(self):
        self.assertEqual(self.normalized, EXPECTED_NORMALIZED)

    def test_normalization_keeps_input_order_and_one_based_line_numbers(self):
        self.assertEqual([v["voucher_id"] for v in self.normalized],
                         ["V-2024-001", "V-2024-002", "V-BIG"])
        for voucher in self.normalized:
            self.assertEqual(
                [entry["line_no"] for entry in voucher["entries"]],
                list(range(1, len(voucher["entries"]) + 1)),
            )

    def test_every_normalized_amount_has_two_decimal_places(self):
        amounts = []
        for voucher in self.normalized:
            amounts.extend((voucher["debit_total"], voucher["credit_total"]))
            for entry in voucher["entries"]:
                amounts.extend((entry["debit"], entry["credit"]))
        self.assertTrue(amounts)
        for amount in amounts:
            self.assertRegex(amount, r"^[0-9]+\.[0-9]{2}$")

    def test_journal_rows_follow_voucher_then_line_order_without_merging(self):
        self.assertEqual(
            [(row["voucher_id"], row["line_no"])
             for row in self.posted["journal"]],
            [("V-2024-001", 1), ("V-2024-001", 2),
             ("V-2024-002", 1), ("V-2024-002", 2), ("V-2024-002", 3),
             ("V-BIG", 1), ("V-BIG", 2)],
        )
        # Two 6601 rows inside the batch stay separate rows; same-code
        # entries are never aggregated into one journal row.
        six_rows = [row for row in self.posted["journal"]
                    if row["account_code"] == "6601"]
        self.assertEqual(len(six_rows), 3)
        self.assertEqual([row["summary"] for row in six_rows],
                         ["报销费用", "费用冲回", "大额费用"])

        # Consecutive same-code lines in one voucher stay unmerged too.
        unmerged = post_vouchers(BASE, make_chart(), [
            make_voucher("V-X", "2024-04-01", entries=[
                make_entry("6601", "第一笔", debit="10.00"),
                make_entry("6601", "第二笔", debit="20.00"),
                make_entry("2202", "对方", debit="0.00", credit="30.00"),
            ])])
        self.assertEqual(
            [(row["account_code"], row["summary"])
             for row in unmerged["journal"]],
            [("6601", "第一笔"), ("6601", "第二笔"),
             ("2202", "对方")],
        )

    def test_journal_and_turnover_amounts_are_two_place_strings(self):
        for row in self.posted["journal"]:
            self.assertRegex(row["debit"], r"^[0-9]+\.[0-9]{2}$")
            self.assertRegex(row["credit"], r"^[0-9]+\.[0-9]{2}$")
        for row in self.posted["accounts"]:
            for field in ("debit_turnover", "credit_turnover",
                          "ending_balance"):
                self.assertRegex(row[field], r"^[0-9]+\.[0-9]{2}$")

    def test_account_summaries_keep_chart_order_including_zero_accounts(self):
        rows = self.posted["accounts"]
        self.assertEqual(
            [(row["code"], row["name"], row["normal_side"]) for row in rows],
            [("1001", "库存现金", "debit"),
             ("2202", "应付账款", "credit"),
             ("6601", "管理费用", "debit"),
             ("5001", "主营业务收入", "credit"),
             ("4001", "停用收入", "credit")],
        )

    def test_unused_active_and_unused_inactive_accounts_report_zero(self):
        by_code = {row["code"]: row for row in self.posted["accounts"]}
        for code in ("5001", "4001"):
            row = by_code[code]
            self.assertEqual(row["debit_turnover"], "0.00")
            self.assertEqual(row["credit_turnover"], "0.00")
            self.assertEqual(row["ending_balance"], "0.00")
            # Zero balance keeps the chart's normal side.
            self.assertEqual(row["ending_side"], row["normal_side"], code)
            self.assertEqual(row["ending_side"], "credit")

    def test_opposing_turnovers_net_to_zero_and_keep_normal_side(self):
        # 2202 is credit-normal: debit 1234.56 vs credit 1234.56 + the big
        # item net to the big item; the small voucher pair itself nets zero.
        by_code = {row["code"]: row for row in self.posted["accounts"]}
        row = by_code["2202"]
        self.assertEqual(row["debit_turnover"], "1234.56")
        self.assertEqual(row["ending_side"], "credit")
        self.assertEqual(row["ending_balance"], BIG)

    def test_reverse_net_flips_the_ending_side(self):
        # 1001 is debit-normal but only has a credit occurrence: the net is
        # negative in the normal direction, so the side flips.
        by_code = {row["code"]: row for row in self.posted["accounts"]}
        row = by_code["1001"]
        self.assertEqual(row["debit_turnover"], "0.00")
        self.assertEqual(row["credit_turnover"], "634.56")
        self.assertEqual(row["ending_side"], "credit")
        self.assertEqual(row["ending_balance"], "634.56")

    def test_debit_normal_account_accumulates_big_amount_exactly(self):
        by_code = {row["code"]: row for row in self.posted["accounts"]}
        row = by_code["6601"]
        self.assertEqual(row["debit_turnover"], "9007199254742227.56")
        self.assertEqual(row["credit_turnover"], "600.00")
        self.assertEqual(row["ending_side"], "debit")
        self.assertEqual(row["ending_balance"], "9007199254741627.56")

    def test_huge_amount_beyond_float_safe_integer_is_exact(self):
        # Document why BIG matters: binary floats round 2**53+1 back down
        # to 2**53, losing the final integer digit entirely.
        self.assertGreater(int(BIG.split(".", 1)[0]), 2 ** 53)
        self.assertEqual(float(BIG), float(2 ** 53))
        self.assertEqual(float(BIG), float(2 ** 53 + 1))  # float artifact
        big_entries = [
            entry for voucher in self.normalized
            if voucher["voucher_id"] == "V-BIG"
            for entry in voucher["entries"]
        ]
        self.assertEqual(big_entries[0]["debit"], BIG)
        self.assertEqual(big_entries[1]["credit"], BIG)
        big_voucher = next(v for v in self.normalized
                           if v["voucher_id"] == "V-BIG")
        self.assertEqual(big_voucher["debit_total"], BIG)
        self.assertEqual(big_voucher["credit_total"], BIG)
        by_code = {row["code"]: row for row in self.posted["accounts"]}
        self.assertEqual(by_code["2202"]["ending_balance"], BIG)

    def test_results_are_json_serializable_with_default_arguments(self):
        self.assertEqual(
            json.loads(json.dumps(self.normalized)), self.normalized)
        self.assertEqual(
            json.loads(json.dumps(self.posted)), self.posted)


# --------------------------------------------------------------------- #
# Deterministic first failure boundary.  Each case plants several
# recognizable problems at once and checks which boundary wins; both
# public entry points must report the same first leaf exception.
# --------------------------------------------------------------------- #
def _chart_error_before_format_error():
    # Broken chart AND a structurally broken batch: the chart wins.
    chart = [{"code": "9001", "name": "坏科目", "normal_side": "sideways",
              "active": True}]
    vouchers = [{"voucher_id": "BROKEN"}]  # missing most fields
    return BASE, chart, vouchers


def _bad_base_currency_before_everything():
    return ("CN", make_chart(), [make_voucher()])


def _format_error_before_all_later_stages():
    # Missing "date" (structural) AND duplicate id AND foreign currency AND
    # unknown/inactive accounts AND a bad amount, all at once.
    broken = {
        "voucher_id": "DUP",
        "currency": "USD",
        "entries": [
            {"account_code": "8888", "summary": "未知且坏金额",
             "debit": "oops", "credit": "0.00"},
            {"account_code": "4001", "summary": "停用",
             "debit": "0.00", "credit": "1.00"},
        ],
    }
    return BASE, make_chart(), [broken, copy.deepcopy(broken)]


def _duplicate_before_currency_and_unknown_account():
    first = make_voucher("V-DUP", "2024-01-01")
    second = make_voucher(
        "V-DUP", "2024-01-02", currency="USD",
        entries=[
            make_entry("8888", "未知", debit="1.00"),
            make_entry("7777", "未知", debit="0.00", credit="1.00"),
        ])
    return BASE, make_chart(), [first, second]


def _currency_before_account_and_amount_defects():
    voucher = make_voucher(
        "V-FX", "2024-01-01", currency="USD",
        entries=[
            make_entry("8888", "未知且坏金额", debit="nope"),
            make_entry("4001", "停用", debit="0.00", credit="1.00"),
        ])
    return BASE, make_chart(), [voucher]


def _unknown_account_before_inactive_account_in_line_order():
    voucher = make_voucher(entries=[
        make_entry("7777", "未知", debit="1.00"),
        make_entry("4001", "停用", debit="0.00", credit="1.00"),
    ])
    return BASE, make_chart(), [voucher]


def _inactive_account_before_unknown_account_in_line_order():
    voucher = make_voucher(entries=[
        make_entry("4001", "停用", debit="1.00"),
        make_entry("7777", "未知", debit="0.00", credit="1.00"),
    ])
    return BASE, make_chart(), [voucher]


def _invalid_amount_syntax():
    voucher = make_voucher(entries=[
        make_entry("6601", "三位小数", debit="1.005"),
        make_entry("2202", "平衡", debit="0.00", credit="1.00"),
    ])
    return BASE, make_chart(), [voucher]


def _invalid_amount_exactly_one_sided_rule():
    # Totals balance (2 == 2); line 1 has both sides zero, which is the
    # only defect reached.
    voucher = make_voucher(entries=[
        make_entry("6601", "双边皆零", debit="0", credit="0.00"),
        make_entry("2202", "贷方一", debit="2.00"),
        make_entry("1001", "借方二", debit="0.00", credit="2.00"),
    ])
    return BASE, make_chart(), [voucher]


def _unbalanced_totals_only():
    voucher = make_voucher(entries=[
        make_entry("6601", "借方五元", debit="5.00"),
        make_entry("2202", "贷方四元", debit="0.00", credit="4.00"),
    ])
    return BASE, make_chart(), [voucher]


FAILURE_CASES = [
    (ChartOfAccountsError, _chart_error_before_format_error()),
    (ChartOfAccountsError, _bad_base_currency_before_everything()),
    (VoucherFormatError, _format_error_before_all_later_stages()),
    (DuplicateVoucherError, _duplicate_before_currency_and_unknown_account()),
    (UnsupportedCurrencyError, _currency_before_account_and_amount_defects()),
    (UnknownAccountError, _unknown_account_before_inactive_account_in_line_order()),
    (InactiveAccountError, _inactive_account_before_unknown_account_in_line_order()),
    (InvalidEntryAmountError, _invalid_amount_syntax()),
    (InvalidEntryAmountError, _invalid_amount_exactly_one_sided_rule()),
    (UnbalancedVoucherError, _unbalanced_totals_only()),
]


class FirstFailureBoundaryTests(PublicContractTestCase):
    def test_first_boundary_for_each_failure_case(self):
        for exc_type, (base, chart, vouchers) in FAILURE_CASES:
            with self.subTest(exc=exc_type.__name__):
                self.assert_leaf_failure_both_entry_points(
                    exc_type, base, chart, copy.deepcopy(vouchers))

    def test_exact_account_code_matching_boundary(self):
        # Codes resolve only by exact match; near literals are unknown.
        for code in ("1001 ", " 1001", "1001x", "100A"):
            with self.subTest(code=code):
                voucher = make_voucher(entries=[
                    make_entry(code, "模糊匹配", debit="1.00"),
                    make_entry("2202", "平衡", credit="1.00"),
                ])
                self.assert_leaf_failure_both_entry_points(
                    UnknownAccountError, BASE, make_chart(), [voucher])

    def test_unknown_code_does_not_shadow_format_error(self):
        # An int account code is a structural defect, never an implicit
        # conversion to the string "1001".
        voucher = make_voucher(entries=[
            make_entry(1001, "数字代码", debit="1.00"),
            make_entry("2202", "平衡", credit="1.00"),
        ])
        self.assert_leaf_failure_both_entry_points(
            VoucherFormatError, BASE, make_chart(), [voucher])

    def test_later_voucher_problem_cannot_advance_first_failure(self):
        # Voucher 2 carries a currency defect that would beat the duplicate
        # on voucher 3; voucher 2 itself must be the first boundary reached.
        vouchers = [
            make_voucher("V-OK", "2024-01-01"),
            make_voucher("V-BAD", "2024-01-02", currency="USD"),
            make_voucher("V-OK", "2024-01-03"),
        ]
        self.assert_leaf_failure_both_entry_points(
            UnsupportedCurrencyError, BASE, make_chart(), vouchers)


# --------------------------------------------------------------------- #
# Published exception hierarchy and released CLI behaviour stay stable.
# --------------------------------------------------------------------- #
class ExceptionHierarchyTests(unittest.TestCase):
    def test_all_leaves_derive_from_the_public_base(self):
        for exc_type in LEAF_ERRORS:
            with self.subTest(exc=exc_type.__name__):
                self.assertTrue(issubclass(exc_type, LedgerEngineError))

    def test_leaves_are_distinct_and_not_related_by_subclassing(self):
        self.assertEqual(len({c for c in LEAF_ERRORS}), len(LEAF_ERRORS))
        for exc_type in LEAF_ERRORS:
            for other in LEAF_ERRORS:
                if other is exc_type:
                    continue
                self.assertFalse(
                    issubclass(exc_type, other),
                    f"{exc_type.__name__} must not subclass "
                    f"{other.__name__}",
                )

    def test_hierarchy_and_entry_points_are_exported_publicly(self):
        for exc_type in LEAF_ERRORS:
            self.assertIn(exc_type.__name__, PUBLIC_NAMES)
        self.assertIn("LedgerEngineError", PUBLIC_NAMES)
        self.assertIn("normalize_vouchers", PUBLIC_NAMES)
        self.assertIn("post_vouchers", PUBLIC_NAMES)


class CliSurfaceTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_version_is_unchanged(self):
        rc, out, err = self._run(["version"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "0.1.0")
        self.assertEqual(out.strip(), __version__)
        self.assertEqual(err, "")

    def test_help_forms_list_the_released_commands(self):
        for argv in (["help"], ["-h"], ["--help"], []):
            with self.subTest(argv=argv):
                rc, out, err = self._run(argv)
                self.assertEqual(rc, 0)
                self.assertIn("version", out)
                self.assertIn("help", out)
                self.assertEqual(err, "")

    def test_unknown_command_returns_two_without_changing_help(self):
        rc, out, err = self._run(["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown command", err)
        self.assertIn("version", err)
        self.assertEqual(out, "")


# --------------------------------------------------------------------- #
# Cross hash-seed reproducibility: fixed JSON bytes from the shuffled
# layout through both entry points must not depend on dict/set hashing.
# --------------------------------------------------------------------- #
class CrossHashSeedTests(unittest.TestCase):
    CHILD = (
        "import json, os, sys;"
        "sys.path.insert(0, os.environ['LEDGER_ENGINE_ROOT']);"
        "from ledger_engine import normalize_vouchers as n, post_vouchers as p;"
        "chart=[{'active':True,'normal_side':'debit','name':'库存现金',"
        "'code':'1001'},{'active':True,'normal_side':'credit',"
        "'name':'应付账款','code':'2202'},"
        "{'active':True,'normal_side':'debit','name':'管理费用',"
        "'code':'6601'},{'active':True,'normal_side':'credit',"
        "'name':'主营业务收入','code':'5001'},"
        "{'active':False,'normal_side':'credit','name':'停用收入',"
        "'code':'4001'}];"
        "big='9007199254740993.00';"
        "e1=[{'credit':'00','debit':'1234.56','summary':'报销费用',"
        "'account_code':'6601'},"
        "{'credit':'1234.56','debit':'0.0','summary':'应付入账',"
        "'account_code':'2202'}];"
        "e2=[{'credit':big,'debit':'00','summary':'大额应付',"
        "'account_code':'2202'},"
        "{'credit':'0.00','debit':big,'summary':'大额费用',"
        "'account_code':'6601'}];"
        "v1={'entries':e1,'currency':'CNY','date':'2024-01-15',"
        "'voucher_id':'V-2024-001'};"
        "v2={'entries':e2,'currency':'CNY','date':'2024-03-05',"
        "'voucher_id':'V-BIG'};"
        "kw={'sort_keys':True,'ensure_ascii':False,"
        "'separators':(',',':')};"
        "out=n('CNY',chart,[v1,v2]);"
        "posted=p('CNY',chart,[v1,v2]);"
        "sys.stdout.buffer.write((json.dumps(out,**kw)+'\\n'"
        "+json.dumps(posted,**kw)).encode('utf-8'))"
    )

    def _run_child(self, seed):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = str(seed)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["LEDGER_ENGINE_ROOT"] = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        completed = subprocess.run(
            [sys.executable, "-c", self.CHILD],
            env=env, capture_output=True, check=True)
        return completed.stdout

    def test_both_entry_points_emit_identical_bytes_under_all_seeds(self):
        reference = self._run_child(0)
        for seed in (1, 7, 12345, "random"):
            with self.subTest(seed=seed):
                self.assertEqual(self._run_child(seed), reference)


if __name__ == "__main__":
    unittest.main(verbosity=2)
