"""Recompute-consistency and input-immutation tests for the public API.

The baseline already pins normalization/posting rules in isolation; this
module locks the *repeatability contract* of the two public entry points
(``normalize_vouchers`` and ``post_vouchers``) as black boxes:

* processing the same ledger set repeatedly returns value-equal results
  whose mutable containers are disjoint at every nesting level;
* semantically equal inputs built as fresh containers with reversed dict
  insertion order produce value-equal, character-identical output under
  fixed JSON serialization parameters;
* deeply mutating one result never leaks into a later call -- the next
  result still matches the untouched first snapshot exactly;
* the raw chart and voucher batch stay layer-by-layer identical after
  both successful and failing calls;
* failures have one deterministic first leaf boundary: a batch carrying
  several distinguishable problems reports exactly the stage's leaf
  exception, identically (type and message) from both entry points,
  with no partial result;
* the ``version``/``help`` CLI surface stays as released.

The success fixture exercises, together: an unused active account, an
inactive unreferenced account, debit- and credit-normal accounts posted
on their opposite side, an account whose occurrences net to zero,
multiple vouchers, and amounts beyond the float safe-integer range
(``2**53 + 1``).

Only the public package surface, the standard library, behavior rather
than message wording and value equality rather than hash ordering are
used; no private stage function is imported.  Runs on CPython 3.10+.
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
    post_vouchers,
)
from ledger_engine.__main__ import main

BASE = "CNY"

# Beyond 2**53 (9007199254740992): float arithmetic would already lose the
# trailing cent here; integer-cents handling must keep it exactly.
HUGE = "9007199254740993.00"
HUGE_NET = "9007199254740869.55"  # HUGE minus the 123.45 repayment below

# Pinned serialization parameters, shared with the baseline suites.
JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}

ENTRY_POINTS = (normalize_vouchers, post_vouchers)


# --------------------------------------------------------------------- #
# Fixture factories -- fresh containers on every call.
# --------------------------------------------------------------------- #
def make_chart():
    """Seven accounts: both normal sides, an unused and an inactive one."""
    return [
        {"active": True, "code": "1001", "name": "库存现金",
         "normal_side": "debit"},
        {"active": True, "code": "2202", "name": "应付账款",
         "normal_side": "credit"},
        {"normal_side": "debit", "name": "管理费用", "active": True,
         "code": "6601"},
        {"code": "6001", "normal_side": "credit", "active": True,
         "name": "主营业务收入"},
        {"name": "备用金", "code": "1221", "normal_side": "debit",
         "active": True},
        {"active": True, "normal_side": "debit", "code": "1901",
         "name": "未使用科目"},
        {"code": "4001", "name": "停用但未引用科目", "active": False,
         "normal_side": "credit"},
    ]


def make_entry(account_code, summary, debit="0.00", credit="0.00"):
    # Insertion order deliberately varies between calls/entries below.
    return {"summary": summary, "credit": credit, "account_code": account_code,
            "debit": debit}


def make_voucher(voucher_id, date_text, entries, currency=BASE):
    return {"entries": entries, "currency": currency, "voucher_id": voucher_id,
            "date": date_text}


def make_batch():
    """Three balanced vouchers exercising every success-path feature.

    * V1 moves the huge amount between a debit-normal expense and a
      credit-normal liability, both on their normal sides;
    * V2 posts the liability on its *debit* side and cash on credit;
    * V3 nets 备用金 to zero, posts income (credit-normal) on its debit
      side hard enough to flip its ending direction, and balances.
    """
    return [
        make_voucher(
            "V-2024-001", "2024-05-10",
            [
                make_entry("6601", "大额费用确认", debit=HUGE),
                make_entry("2202", "大额挂账", credit=HUGE),
            ],
        ),
        make_voucher(
            "V-2024-002", "2024-05-11",
            [
                make_entry("2202", "偿还欠款(借方发生)", debit="123.45"),
                make_entry("1001", "现金支付", credit="123.45"),
            ],
        ),
        make_voucher(
            "V-2024-003", "2024-05-12",
            [
                make_entry("1221", "划出备用金", debit="50.00"),
                make_entry("1221", "收回备用金", credit="50.00"),
                make_entry("6001", "确认收入", credit="30.00"),
                make_entry("6001", "红字冲回收入", debit="80.00"),
                make_entry("1001", "补差现金", credit="50.00"),
            ],
        ),
    ]


# --------------------------------------------------------------------- #
# Structural helpers (test utilities only -- no private API is touched).
# --------------------------------------------------------------------- #
def mutable_ids(value):
    """Ids of every dict/list in ``value``, at all nesting levels."""
    found: set[int] = set()

    def walk(node):
        if isinstance(node, (dict, list)):
            found.add(id(node))
            children = node.values() if isinstance(node, dict) else node
            for child in children:
                walk(child)

    walk(value)
    return found


def reversed_key_order(value):
    """Deep copy with every dict's key insertion order reversed.

    Lists keep their order (list order is semantic); only dict insertion
    order and container identity change.
    """
    if isinstance(value, dict):
        return {key: reversed_key_order(val)
                for key, val in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reversed_key_order(item) for item in value]
    return value


def assert_layered_equal(testcase, actual, expected, path="root"):
    """Assert equality node by node, including dict insertion order."""
    testcase.assertEqual(type(actual), type(expected), path)
    if isinstance(actual, dict):
        testcase.assertEqual(list(actual.keys()), list(expected.keys()), path)
        for key in actual:
            assert_layered_equal(testcase, actual[key], expected[key],
                                 f"{path}.{key}")
    elif isinstance(actual, list):
        testcase.assertEqual(len(actual), len(expected), path)
        for index, (a_item, b_item) in enumerate(zip(actual, expected)):
            assert_layered_equal(testcase, a_item, b_item, f"{path}[{index}]")
    else:
        testcase.assertEqual(actual, expected, path)


def corrupt_deep(value):
    """Aggressively mutate every reachable mutable container in place."""
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if isinstance(child, (dict, list)):
                corrupt_deep(child)
            else:
                value[key] = ("POLLUTED", None)[0]
        value["__injected_by_test__"] = [1, 2, 3]
    elif isinstance(value, list):
        for child in value:
            corrupt_deep(child)
        if value:
            value.reverse()
        value.append("__injected_by_test__")


# --------------------------------------------------------------------- #
# Repeated processing: equal values, disjoint containers, stable inputs.
# --------------------------------------------------------------------- #
class RecomputeConsistencyTests(unittest.TestCase):
    def call_each(self, entry_point):
        """Run one entry point three times on equivalent fresh fixtures."""
        return [
            entry_point(BASE, make_chart(), make_batch()) for _ in range(3)
        ]

    def test_normalize_repeated_runs_are_equal_with_disjoint_containers(self):
        results = self.call_each(normalize_vouchers)
        for result in results[1:]:
            self.assertEqual(result, results[0])
        first_ids, second_ids = (mutable_ids(r) for r in results[:2])
        self.assertTrue(first_ids.isdisjoint(second_ids),
                        "no dict/list instance may be shared between calls")
        # Per-level identity checks make the disjointness concrete.
        self.assertIsNot(results[0], results[1])
        self.assertIsNot(results[0][0], results[1][0])
        self.assertIsNot(results[0][0]["entries"], results[1][0]["entries"])
        self.assertIsNot(results[0][0]["entries"][0],
                         results[1][0]["entries"][0])

    def test_post_repeated_runs_are_equal_with_disjoint_containers(self):
        results = self.call_each(post_vouchers)
        for result in results[1:]:
            self.assertEqual(result, results[0])
        first_ids, second_ids = (mutable_ids(r) for r in results[:2])
        self.assertTrue(first_ids.isdisjoint(second_ids),
                        "no dict/list instance may be shared between calls")
        self.assertIsNot(results[0], results[1])
        self.assertIsNot(results[0]["journal"], results[1]["journal"])
        self.assertIsNot(results[0]["accounts"], results[1]["accounts"])
        self.assertIsNot(results[0]["journal"][0], results[1]["journal"][0])
        self.assertIsNot(results[0]["accounts"][0], results[1]["accounts"][0])

    def test_results_never_share_containers_with_their_raw_inputs(self):
        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                chart, batch = make_chart(), make_batch()
                result = entry_point(BASE, chart, batch)
                input_ids = mutable_ids(chart) | mutable_ids(batch)
                self.assertTrue(mutable_ids(result).isdisjoint(input_ids),
                                "result containers must be freshly built")

    def test_normalize_equivalent_inputs_regardless_of_dict_insertion_order(self):
        plain = (BASE, make_chart(), make_batch())
        shuffled = (BASE, reversed_key_order(plain[1]),
                    reversed_key_order(plain[2]))
        # The two inputs are semantically equal but distinct objects...
        self.assertEqual(plain[1:], shuffled[1:])
        self.assertIsNot(plain[1], shuffled[1])
        self.assertIsNot(plain[2], shuffled[2])
        first = normalize_vouchers(*plain)
        second = normalize_vouchers(*shuffled)
        self.assertEqual(first, second)
        self.assertTrue(mutable_ids(first).isdisjoint(mutable_ids(second)))
        self.assertEqual(json.dumps(first, **JSON_KW),
                         json.dumps(second, **JSON_KW))

    def test_post_equivalent_inputs_regardless_of_dict_insertion_order(self):
        plain = (BASE, make_chart(), make_batch())
        shuffled = (BASE, reversed_key_order(plain[1]),
                    reversed_key_order(plain[2]))
        first = post_vouchers(*plain)
        second = post_vouchers(*shuffled)
        self.assertEqual(first, second)
        self.assertTrue(mutable_ids(first).isdisjoint(mutable_ids(second)))
        self.assertEqual(json.dumps(first, **JSON_KW),
                         json.dumps(second, **JSON_KW))

    def test_mutating_a_normalized_result_cannot_reach_the_next_call(self):
        chart, batch = make_chart(), make_batch()
        first = normalize_vouchers(BASE, chart, batch)
        snapshot = copy.deepcopy(first)
        corrupt_deep(first)
        second = normalize_vouchers(BASE, chart, batch)
        self.assertEqual(second, snapshot)
        self.assertNotEqual(first, snapshot)
        # The reused raw inputs are still exactly as originally supplied.
        assert_layered_equal(self, chart, make_chart())
        assert_layered_equal(self, batch, make_batch())

    def test_mutating_a_posted_result_cannot_reach_the_next_call(self):
        chart, batch = make_chart(), make_batch()
        first = post_vouchers(BASE, chart, batch)
        snapshot = copy.deepcopy(first)
        corrupt_deep(first)
        second = post_vouchers(BASE, chart, batch)
        self.assertEqual(second, snapshot)
        self.assertNotEqual(first, snapshot)
        assert_layered_equal(self, chart, make_chart())
        assert_layered_equal(self, batch, make_batch())

    def test_raw_inputs_stay_layer_identical_after_success(self):
        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                chart, batch = make_chart(), make_batch()
                chart_copy, batch_copy = copy.deepcopy(chart), copy.deepcopy(
                    batch)
                entry_point(BASE, chart, batch)
                assert_layered_equal(self, chart, chart_copy)
                assert_layered_equal(self, batch, batch_copy)


# --------------------------------------------------------------------- #
# Success contract on the rich fixture: normalization output shape.
# --------------------------------------------------------------------- #
class NormalizedContractTests(unittest.TestCase):
    def setUp(self):
        self.result = normalize_vouchers(BASE, make_chart(), make_batch())

    def test_voucher_and_entry_input_order_is_preserved(self):
        self.assertEqual([v["voucher_id"] for v in self.result],
                         ["V-2024-001", "V-2024-002", "V-2024-003"])
        self.assertEqual(
            [[e["account_code"] for e in v["entries"]] for v in self.result],
            [["6601", "2202"],
             ["2202", "1001"],
             ["1221", "1221", "6001", "6001", "1001"]],
        )

    def test_field_insertion_order_matches_the_public_contract(self):
        self.assertEqual(
            list(self.result[0]),
            ["voucher_id", "date", "currency", "entries",
             "debit_total", "credit_total"],
        )
        self.assertEqual(
            list(self.result[0]["entries"][0]),
            ["line_no", "account_code", "account_name", "normal_side",
             "summary", "debit", "credit"],
        )

    def test_line_numbers_start_at_one_per_voucher(self):
        for voucher in self.result:
            self.assertEqual([e["line_no"] for e in voucher["entries"]],
                             list(range(1, len(voucher["entries"]) + 1)))

    def test_every_amount_string_has_exactly_two_decimal_places(self):
        two_places = r"^[0-9]+\.[0-9]{2}$"
        for voucher in self.result:
            self.assertRegex(voucher["debit_total"], two_places)
            self.assertRegex(voucher["credit_total"], two_places)
            for entry in voucher["entries"]:
                self.assertRegex(entry["debit"], two_places)
                self.assertRegex(entry["credit"], two_places)

    def test_huge_amount_and_totals_keep_full_precision(self):
        first = self.result[0]
        self.assertEqual(first["entries"][0]["debit"], HUGE)
        self.assertEqual(first["entries"][1]["credit"], HUGE)
        self.assertEqual(first["debit_total"], HUGE)
        self.assertEqual(first["credit_total"], HUGE)

    def test_voucher_three_totals_balance_exactly(self):
        third = self.result[2]
        self.assertEqual(third["debit_total"], "130.00")
        self.assertEqual(third["credit_total"], "130.00")

    def test_normal_side_is_reported_from_the_chart_on_opposite_use(self):
        by_code = {row["code"]: row for row in make_chart()}
        for voucher in self.result:
            for entry in voucher["entries"]:
                self.assertEqual(entry["normal_side"],
                                 by_code[entry["account_code"]]["normal_side"])
                self.assertEqual(entry["account_name"],
                                 by_code[entry["account_code"]]["name"])
        # 2202 is credit-normal yet posted on the debit side in voucher 2...
        self.assertEqual(self.result[1]["entries"][0]["account_code"], "2202")
        self.assertEqual(self.result[1]["entries"][0]["debit"], "123.45")
        self.assertEqual(self.result[1]["entries"][0]["normal_side"],
                         "credit")
        # ...and 6001 is credit-normal yet posted on the debit side in v3.
        self.assertEqual(self.result[2]["entries"][3]["account_code"], "6001")
        self.assertEqual(self.result[2]["entries"][3]["debit"], "80.00")
        self.assertEqual(self.result[2]["entries"][3]["normal_side"],
                         "credit")


# --------------------------------------------------------------------- #
# Success contract on the rich fixture: posting output shape.
# --------------------------------------------------------------------- #
class PostedContractTests(unittest.TestCase):
    def setUp(self):
        self.result = post_vouchers(BASE, make_chart(), make_batch())

    def test_top_level_shape(self):
        self.assertEqual(set(self.result),
                         {"base_currency", "journal", "accounts"})
        self.assertEqual(list(self.result),
                         ["base_currency", "journal", "accounts"])
        self.assertEqual(self.result["base_currency"], BASE)

    def test_journal_rows_follow_voucher_then_line_order_without_merging(self):
        rows = self.result["journal"]
        self.assertEqual(
            [(r["voucher_id"], r["line_no"], r["account_code"])
             for r in rows],
            [("V-2024-001", 1, "6601"), ("V-2024-001", 2, "2202"),
             ("V-2024-002", 1, "2202"), ("V-2024-002", 2, "1001"),
             ("V-2024-003", 1, "1221"), ("V-2024-003", 2, "1221"),
             ("V-2024-003", 3, "6001"), ("V-2024-003", 4, "6001"),
             ("V-2024-003", 5, "1001")],
        )
        # Consecutive same-account rows stay separate (no aggregation).
        self.assertEqual(
            [(r["account_code"], r["summary"]) for r in rows[4:8]],
            [("1221", "划出备用金"), ("1221", "收回备用金"),
             ("6001", "确认收入"), ("6001", "红字冲回收入")],
        )

    def test_journal_field_order_and_two_place_amounts(self):
        two_places = r"^[0-9]+\.[0-9]{2}$"
        for row in self.result["journal"]:
            self.assertEqual(
                list(row),
                ["voucher_id", "date", "line_no", "account_code",
                 "account_name", "summary", "debit", "credit"],
            )
            self.assertRegex(row["debit"], two_places)
            self.assertRegex(row["credit"], two_places)
        self.assertEqual(self.result["journal"][0]["debit"], HUGE)

    def test_account_summaries_keep_chart_order_including_unused_inactive(self):
        self.assertEqual([r["code"] for r in self.result["accounts"]],
                         ["1001", "2202", "6601", "6001", "1221", "1901",
                          "4001"])
        for row in self.result["accounts"]:
            self.assertEqual(
                list(row),
                ["code", "name", "normal_side", "debit_turnover",
                 "credit_turnover", "ending_side", "ending_balance"],
            )

    def rows_by_code(self):
        return {row["code"]: row for row in self.result["accounts"]}

    def test_huge_amount_turnover_and_balance_keep_full_precision(self):
        rows = self.rows_by_code()
        self.assertEqual(rows["6601"]["debit_turnover"], HUGE)
        self.assertEqual(rows["6601"]["ending_side"], "debit")
        self.assertEqual(rows["6601"]["ending_balance"], HUGE)
        self.assertEqual(rows["2202"]["debit_turnover"], "123.45")
        self.assertEqual(rows["2202"]["credit_turnover"], HUGE)
        self.assertEqual(rows["2202"]["ending_side"], "credit")
        self.assertEqual(rows["2202"]["ending_balance"], HUGE_NET)

    def test_zero_net_account_keeps_its_normal_side_with_zero_balance(self):
        row = self.rows_by_code()["1221"]
        self.assertEqual(row["debit_turnover"], "50.00")
        self.assertEqual(row["credit_turnover"], "50.00")
        self.assertEqual(row["ending_side"], "debit")
        self.assertEqual(row["ending_balance"], "0.00")

    def test_reverse_net_flips_the_ending_direction(self):
        # 6001 is credit-normal: 80.00 debit vs 30.00 credit -> debit 50.00.
        income = self.rows_by_code()["6001"]
        self.assertEqual(income["debit_turnover"], "80.00")
        self.assertEqual(income["credit_turnover"], "30.00")
        self.assertEqual(income["ending_side"], "debit")
        self.assertEqual(income["ending_balance"], "50.00")
        # 1001 is debit-normal but only ever credited -> flips to credit.
        cash = self.rows_by_code()["1001"]
        self.assertEqual(cash["debit_turnover"], "0.00")
        self.assertEqual(cash["credit_turnover"], "173.45")
        self.assertEqual(cash["ending_side"], "credit")
        self.assertEqual(cash["ending_balance"], "173.45")

    def test_unused_and_inactive_accounts_report_full_zero_rows(self):
        for code, side in (("1901", "debit"), ("4001", "credit")):
            row = self.rows_by_code()[code]
            self.assertEqual(row["normal_side"], side)
            self.assertEqual(row["debit_turnover"], "0.00")
            self.assertEqual(row["credit_turnover"], "0.00")
            self.assertEqual(row["ending_side"], side)
            self.assertEqual(row["ending_balance"], "0.00")


# --------------------------------------------------------------------- #
# Byte-level serialization reproducibility.
# --------------------------------------------------------------------- #
class SerializationDeterminismTests(unittest.TestCase):
    def test_fixed_json_text_is_stable_across_runs_and_dict_order(self):
        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                runs = [
                    entry_point(BASE, make_chart(), make_batch()),
                    entry_point(
                        BASE,
                        reversed_key_order(make_chart()),
                        reversed_key_order(make_batch()),
                    ),
                    entry_point(BASE, make_chart(), make_batch()),
                ]
                texts = [json.dumps(result, **JSON_KW) for result in runs]
                self.assertEqual(texts[0], texts[1])
                self.assertEqual(texts[0], texts[2])
                self.assertEqual(texts[0].encode("utf-8"),
                                 texts[1].encode("utf-8"))
                # Large-amount arithmetic survives verbatim in the bytes.
                self.assertIn(HUGE, texts[0])
                if entry_point is post_vouchers:
                    # Netting the huge turnover with the 123.45 repayment is
                    # a posting-only derivation.
                    self.assertIn(HUGE_NET, texts[0])

    def test_output_round_trips_through_json(self):
        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                result = entry_point(BASE, make_chart(), make_batch())
                self.assertEqual(json.loads(json.dumps(result, **JSON_KW)),
                                 result)


# --------------------------------------------------------------------- #
# Deterministic first leaf failure, shared identically by both entries.
# --------------------------------------------------------------------- #
class FirstFailureBoundaryTests(unittest.TestCase):
    def assert_boundary(self, expected_exc, chart, batch):
        """Both public entries raise the same leaf type and message.

        Also asserts the call returns nothing (it raises) and leaves the
        raw chart and batch layer-by-layer identical, so no partial work
        is observable.
        """
        messages = []
        for entry_point in ENTRY_POINTS:
            chart_snapshot = copy.deepcopy(chart)
            batch_snapshot = copy.deepcopy(batch)
            with self.assertRaises(LedgerEngineError) as caught:
                entry_point(BASE, chart, batch)
            self.assertIs(type(caught.exception), expected_exc,
                          f"{entry_point.__name__} must report the leaf "
                          f"{expected_exc.__name__}, got "
                          f"{type(caught.exception).__name__}")
            messages.append(str(caught.exception))
            assert_layered_equal(self, chart, chart_snapshot)
            assert_layered_equal(self, batch, batch_snapshot)
        self.assertEqual(messages[0], messages[1],
                         "normalize and post must report the same first "
                         "leaf exception, message included")

    def test_chart_error_outranks_every_voucher_problem(self):
        # Broken chart AND a non-list batch: chart parsing comes first.
        chart = make_chart()
        chart.append({"code": "1001", "name": "重复科目",
                      "normal_side": "debit", "active": True})
        self.assert_boundary(ChartOfAccountsError, chart, None)

    def test_format_error_outranks_duplicate_and_business_problems(self):
        malformed = make_voucher(
            "V-BAD", "2024-05-10",
            [make_entry("6601", "只有一条分录", debit="1.00")],
        )
        malformed["currency"] = 123  # a second, distinguishable shape defect
        # Later vouchers additionally repeat an id: must remain masked.
        batch = [malformed,
                 make_voucher("V-2024-001", "2024-05-11", [
                     make_entry("6601", "借", debit="1.00"),
                     make_entry("2202", "贷", credit="1.00")]),
                 make_voucher("V-2024-001", "2024-05-12", [
                     make_entry("6601", "借", debit="1.00"),
                     make_entry("2202", "贷", credit="1.00")])]
        self.assert_boundary(VoucherFormatError, make_chart(), batch)

    def test_duplicate_id_outranks_currency_and_entry_defects(self):
        good_first = make_voucher("V-2024-001", "2024-05-10", [
            make_entry("6601", "借", debit="100.00"),
            make_entry("2202", "贷", credit="100.00"),
        ])
        # Same id, foreign currency, unknown + inactive accounts, junk
        # amounts and unbalanced totals -- duplicate is reached first.
        duplicate = make_voucher("V-2024-001", "2024-05-11", [
            make_entry("9999", "未知科目", debit="not-money"),
            make_entry("4001", "停用科目", credit="also-junk"),
        ], currency="USD")
        self.assert_boundary(DuplicateVoucherError, make_chart(),
                             [good_first, duplicate])

    def test_unsupported_currency_outranks_account_and_amount_defects(self):
        voucher = make_voucher("V-FX", "2024-05-10", [
            make_entry("9999", "未知且金额坏", debit="1.005"),
            make_entry("4001", "停用", credit="0.002"),
        ], currency="USD")
        self.assert_boundary(UnsupportedCurrencyError, make_chart(), [voucher])

    def test_unknown_account_outranks_inactive_amount_and_balance(self):
        voucher = make_voucher("V-U", "2024-05-10", [
            make_entry("9999", "未知且金额坏", debit="1.005"),
            make_entry("4001", "停用科目", credit="1.00"),
            make_entry("2202", "刻意不平", credit="9.00"),
        ])
        self.assert_boundary(UnknownAccountError, make_chart(), [voucher])

    def test_inactive_account_outranks_amount_and_balance_defects(self):
        voucher = make_voucher("V-I", "2024-05-10", [
            make_entry("4001", "停用且金额坏", debit="1.005"),
            make_entry("9999", "未知科目", credit="1.00"),
            make_entry("2202", "刻意不平", credit="9.00"),
        ])
        self.assert_boundary(InactiveAccountError, make_chart(), [voucher])

    def test_invalid_amount_outranks_unbalanced_totals(self):
        voucher = make_voucher("V-A", "2024-05-10", [
            make_entry("6601", "三位小数", debit="1.005"),
            make_entry("2202", "金额不等", credit="9.00"),
        ])
        self.assert_boundary(InvalidEntryAmountError, make_chart(), [voucher])

    def test_unbalanced_voucher_is_the_last_boundary(self):
        voucher = make_voucher("V-B", "2024-05-10", [
            make_entry("6601", "借五元", debit="5.00"),
            make_entry("2202", "贷四元", credit="4.00"),
        ])
        self.assert_boundary(UnbalancedVoucherError, make_chart(), [voucher])

    def test_exact_account_matching_and_base_currency_remain_strict(self):
        # Near-match codes must resolve to UnknownAccountError, never to the
        # similarly named chart account, from both entries alike.
        for code in ("1001 ", " 1001", "66O1"):
            with self.subTest(code=code):
                voucher = make_voucher("V-E", "2024-05-10", [
                    make_entry(code, "近似编码", debit="1.00"),
                    make_entry("2202", "平衡", credit="1.00"),
                ])
                self.assert_boundary(UnknownAccountError, make_chart(),
                                     [voucher])
        for currency in ("usd", "RMB", "CN", "CNYY"):
            with self.subTest(currency=currency):
                voucher = make_voucher("V-C", "2024-05-10", [
                    make_entry("6601", "借", debit="1.00"),
                    make_entry("2202", "贷", credit="1.00"),
                ], currency=currency)
                self.assert_boundary(UnsupportedCurrencyError, make_chart(),
                                     [voucher])


# --------------------------------------------------------------------- #
# CLI surface: version and help behavior stays as released.
# --------------------------------------------------------------------- #
class CliSurfaceTests(unittest.TestCase):
    def test_version_prints_0_1_0(self):
        self.assertEqual(__version__, "0.1.0")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["version"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "0.1.0\n")

    def test_help_variants_are_identical_and_list_both_commands(self):
        texts = {}
        for command in ("help", "-h", "--help"):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = main([command])
            self.assertEqual(rc, 0)
            texts[command] = out.getvalue()
        self.assertEqual(texts["help"], texts["-h"])
        self.assertEqual(texts["help"], texts["--help"])
        self.assertIn("version", texts["help"])
        self.assertIn("help", texts["help"])
        self.assertIn("usage:", texts["help"])

    def test_no_args_prints_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main([])
        self.assertEqual(rc, 0)
        self.assertIn("usage:", out.getvalue())

    def test_unknown_command_still_fails_with_code_two(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown command", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
