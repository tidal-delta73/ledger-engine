"""Regression tests for the shared ledger aggregation boundary.

The public suites pin the observable contracts of ``post_vouchers`` and
``build_trial_balance``; this module pins the *internal* seam introduced
so that per-account debit/credit accumulation exists in exactly one
place, :func:`ledger_engine.vouchers.ledger.accumulate_turnovers`:

* both public entry points reference that one function (no second copy
  they could drift against), and their turnover columns reproduce its
  integer-cents fact account by account;
* the fact itself is chart-ordered, zero-initialized for every active,
  untouched and inactive account, accumulated in voucher-then-line order
  in integer cents (exact beyond the float safe-integer range), and
  frozen so no mutable state can be shared;
* the two presentations stay private to each entry point -- posting's
  ``normal_side`` ending rule never appears in a trial-balance row and
  vice versa -- and the two public results share no mutable container;
* empty batch, empty chart and zero-net cases match the public rows;
* the shared fact is not mutated and is rebuilt fresh on every call.

Standard library only; CPython 3.10+.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import unittest

from ledger_engine import build_trial_balance, post_vouchers
from ledger_engine.vouchers import (
    amounts,
    chart as chart_mod,
    entries,
    ledger,
    posting,
    structure,
    trial_balance,
)

BASE = "CNY"

# Beyond 2**53 (9007199254740992): a float could not keep the last cent.
HUGE = "9007199254740993.00"


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
        {"code": "4001", "name": "停用科目", "normal_side": "credit",
         "active": False},
    ]


def make_entry(account_code, summary, debit="0.00", credit="0.00"):
    return {"account_code": account_code, "summary": summary,
            "debit": debit, "credit": credit}


def make_voucher(voucher_id, date_text, rows, currency=BASE):
    return {"voucher_id": voucher_id, "date": date_text, "currency": currency,
            "entries": rows}


def make_batch():
    return [
        make_voucher(
            "V-2024-001", "2024-05-10",
            [
                make_entry("6601", "大额费用", debit=HUGE),
                make_entry("2202", "大额挂账", credit=HUGE),
            ],
        ),
        make_voucher(
            "V-2024-002", "2024-05-11",
            [
                make_entry("2202", "偿还欠款", debit="123.45"),
                make_entry("1001", "现金支付", credit="123.45"),
            ],
        ),
        make_voucher(
            "V-2024-003", "2024-05-12",
            [
                # 1221 nets to zero but must still show both turnovers.
                make_entry("6001", "红字冲回收入", debit="80.00"),
                make_entry("6001", "确认收入", credit="30.00"),
                make_entry("1001", "补差现金", credit="50.00"),
            ],
        ),
    ]


def validated_fixture(chart_raw, batch_raw):
    """Run the raw inputs through the shared stages, without validation
    orchestration, so the aggregator can be tested on its own."""
    chart = chart_mod.parse_chart(BASE, chart_raw)
    validated = []
    for index, raw_voucher in enumerate(batch_raw):
        structured = structure.structure_voucher(raw_voucher, index)
        body = entries.validate_entries(structured, chart, index)
        validated.append((structured, body))
    return chart, validated


def mutable_ids(value):
    found: set[int] = set()

    def walk(node):
        if isinstance(node, (dict, list)):
            found.add(id(node))
            children = node.values() if isinstance(node, dict) else node
            for child in children:
                walk(child)
    walk(value)
    return found


# --------------------------------------------------------------------- #
# The accumulation fact itself.
# --------------------------------------------------------------------- #
class AccumulateTurnoversTests(unittest.TestCase):
    def test_one_frozen_row_per_chart_account_in_chart_order(self):
        chart, validated = validated_fixture(make_chart(), make_batch())
        rows = ledger.accumulate_turnovers(chart, validated)
        self.assertIsInstance(rows, tuple)
        self.assertEqual([row.account.code for row in rows],
                         ["1001", "2202", "6601", "6001", "1901", "4001"])
        for row in rows:
            self.assertIsInstance(row, ledger.AccountTurnover)
            self.assertIsInstance(row.account, chart_mod.Account)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                row.debit_cents = 1

    def test_untouched_and_inactive_accounts_are_zero_initialized(self):
        chart, validated = validated_fixture(make_chart(), make_batch())
        rows = {row.account.code: row
                for row in ledger.accumulate_turnovers(chart, validated)}
        for code in ("1901", "4001"):
            self.assertEqual((rows[code].debit_cents, rows[code].credit_cents),
                             (0, 0))

    def test_entries_accumulate_in_voucher_then_line_order(self):
        chart, validated = validated_fixture(make_chart(), make_batch())
        rows = {row.account.code: row
                for row in ledger.accumulate_turnovers(chart, validated)}
        self.assertEqual((rows["2202"].debit_cents, rows["2202"].credit_cents),
                         (12345, 900719925474099300))
        self.assertEqual((rows["1001"].debit_cents, rows["1001"].credit_cents),
                         (0, 17345))
        self.assertEqual((rows["6001"].debit_cents, rows["6001"].credit_cents),
                         (8000, 3000))
        self.assertEqual((rows["6601"].debit_cents, rows["6601"].credit_cents),
                         (900719925474099300, 0))

    def test_zero_net_keeps_both_non_zero_turnovers(self):
        batch = [make_voucher("V-1", "2024-05-10", [
            make_entry("6601", "借", debit="50.00"),
            make_entry("1001", "贷", credit="50.00"),
        ])]
        chart = chart_mod.parse_chart(BASE, make_chart())
        structured = structure.structure_voucher(batch[0], 0)
        body = entries.validate_entries(structured, chart, 0)
        # A second balanced voucher on 1001 both ways nets it to zero.
        batch.append(make_voucher("V-2", "2024-05-11", [
            make_entry("1001", "收回", debit="50.00"),
            make_entry("6601", "冲回", credit="50.00"),
        ]))
        structured2 = structure.structure_voucher(batch[1], 1)
        body2 = entries.validate_entries(structured2, chart, 1)
        rows = {
            row.account.code: row
            for row in ledger.accumulate_turnovers(
                chart, [(structured, body), (structured2, body2)])
        }
        self.assertEqual((rows["1001"].debit_cents, rows["1001"].credit_cents),
                         (5000, 5000))

    def test_empty_batch_zeroes_every_account_in_chart_order(self):
        chart, validated = validated_fixture(make_chart(), [])
        rows = ledger.accumulate_turnovers(chart, validated)
        self.assertEqual([row.account.code for row in rows],
                         ["1001", "2202", "6601", "6001", "1901", "4001"])
        self.assertTrue(all(
            row.debit_cents == 0 and row.credit_cents == 0 for row in rows))

    def test_empty_chart_and_batch_return_empty_tuple(self):
        chart, validated = validated_fixture([], [])
        self.assertEqual(ledger.accumulate_turnovers(chart, validated), ())

    def test_huge_amounts_keep_full_integer_precision(self):
        big = "123456789012345678901234567890.01"
        chart, validated = validated_fixture(
            make_chart(),
            [make_voucher("V-1", "2024-05-10", [
                make_entry("6601", "大额", debit=big),
                make_entry("2202", "大额", credit=big),
            ])],
        )
        rows = {row.account.code: row
                for row in ledger.accumulate_turnovers(chart, validated)}
        huge_cents = 12345678901234567890123456789001
        self.assertEqual(rows["6601"].debit_cents, huge_cents)
        self.assertEqual(rows["2202"].credit_cents, huge_cents)
        self.assertGreater(huge_cents, 2 ** 63)

    def test_repeated_calls_return_equal_fresh_facts_without_input_mutation(self):
        chart_raw, batch_raw = make_chart(), make_batch()
        chart_a, validated_a = validated_fixture(chart_raw, batch_raw)
        chart_b, validated_b = validated_fixture(
            copy.deepcopy(chart_raw), copy.deepcopy(batch_raw))
        first = ledger.accumulate_turnovers(chart_a, validated_a)
        second = ledger.accumulate_turnovers(chart_b, validated_b)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertTrue(all(a is not b for a, b in zip(first, second)))
        # The raw inputs stay untouched.
        self.assertEqual(chart_raw, make_chart())
        self.assertEqual(batch_raw, make_batch())


# --------------------------------------------------------------------- #
# Both public entries consume that one fact -- no second accumulation.
# --------------------------------------------------------------------- #
class SingleSourceTests(unittest.TestCase):
    def test_both_entries_reference_the_same_accumulator(self):
        # Drift guard: posting and trial balance must call the very same
        # function object rather than carry private accumulation loops.
        self.assertIs(posting.accumulate_turnovers,
                      ledger.accumulate_turnovers)
        self.assertIs(trial_balance.accumulate_turnovers,
                      ledger.accumulate_turnovers)

    def test_public_turnover_columns_reproduce_the_shared_fact(self):
        chart, validated = validated_fixture(make_chart(), make_batch())
        fact = {
            row.account.code: row
            for row in ledger.accumulate_turnovers(chart, validated)
        }
        posted = post_vouchers(BASE, make_chart(), make_batch())
        balanced = build_trial_balance(BASE, make_chart(), make_batch())
        posted_rows = {row["code"]: row for row in posted["accounts"]}
        balance_rows = {row["code"]: row for row in balanced["accounts"]}
        self.assertEqual(set(posted_rows), set(fact))
        self.assertEqual(set(balance_rows), set(fact))
        for code, row in fact.items():
            self.assertEqual(posted_rows[code]["debit_turnover"],
                             amounts.format_cents(row.debit_cents))
            self.assertEqual(posted_rows[code]["credit_turnover"],
                             amounts.format_cents(row.credit_cents))
            self.assertEqual(balance_rows[code]["debit_turnover"],
                             amounts.format_cents(row.debit_cents))
            self.assertEqual(balance_rows[code]["credit_turnover"],
                             amounts.format_cents(row.credit_cents))

    def test_journal_and_fact_totals_agree_completely(self):
        # Every journal debit/credit cent must be accounted for by the
        # fact -- the fact is what posting posted.
        chart, validated = validated_fixture(make_chart(), make_batch())
        fact = ledger.accumulate_turnovers(chart, validated)
        posted = post_vouchers(BASE, make_chart(), make_batch())
        journal_debit = sum(
            amounts.parse_cents(r["debit"], where="journal")
            for r in posted["journal"])
        journal_credit = sum(
            amounts.parse_cents(r["credit"], where="journal")
            for r in posted["journal"])
        self.assertEqual(journal_debit, sum(r.debit_cents for r in fact))
        self.assertEqual(journal_credit, sum(r.credit_cents for r in fact))

    def test_trial_totals_are_the_sums_of_the_shared_fact(self):
        chart, validated = validated_fixture(make_chart(), make_batch())
        fact = ledger.accumulate_turnovers(chart, validated)
        totals = build_trial_balance(
            BASE, make_chart(), make_batch())["totals"]
        ending_debit = sum(max(0, r.debit_cents - r.credit_cents) for r in fact)
        ending_credit = sum(max(0, r.credit_cents - r.debit_cents) for r in fact)
        self.assertEqual(
            totals["debit_turnover"],
            amounts.format_cents(sum(r.debit_cents for r in fact)))
        self.assertEqual(
            totals["credit_turnover"],
            amounts.format_cents(sum(r.credit_cents for r in fact)))
        self.assertEqual(totals["ending_debit"],
                         amounts.format_cents(ending_debit))
        self.assertEqual(totals["ending_credit"],
                         amounts.format_cents(ending_credit))


# --------------------------------------------------------------------- #
# Presentations stay private to each entry point; containers stay apart.
# --------------------------------------------------------------------- #
class PresentationIsolationTests(unittest.TestCase):
    def setUp(self):
        self.posted = post_vouchers(BASE, make_chart(), make_batch())
        self.balanced = build_trial_balance(BASE, make_chart(), make_batch())

    def test_each_shape_keeps_only_its_own_ending_columns(self):
        for row in self.posted["accounts"]:
            self.assertIn("ending_side", row)
            self.assertIn("ending_balance", row)
            self.assertNotIn("ending_debit", row)
            self.assertNotIn("ending_credit", row)
        for row in self.balanced["accounts"]:
            self.assertIn("ending_debit", row)
            self.assertIn("ending_credit", row)
            self.assertNotIn("ending_side", row)
            self.assertNotIn("ending_balance", row)

    def test_same_fact_different_presentation_on_non_zero_and_zero_nets(self):
        posted_rows = {r["code"]: r for r in self.posted["accounts"]}
        balance_rows = {r["code"]: r for r in self.balanced["accounts"]}
        # 2202 is credit-normal with a huge credit net: both show credit.
        self.assertEqual(posted_rows["2202"]["ending_side"], "credit")
        self.assertEqual(posted_rows["2202"]["ending_balance"],
                         balance_rows["2202"]["ending_credit"])
        self.assertEqual(balance_rows["2202"]["ending_debit"], "0.00")
        # 6001 is credit-normal but has a debit net: posting flips the
        # side, the trial balance ignores normal_side.
        self.assertEqual(posted_rows["6001"]["ending_side"], "debit")
        self.assertEqual(posted_rows["6001"]["ending_balance"], "50.00")
        self.assertEqual(balance_rows["6001"]["ending_debit"], "50.00")
        self.assertEqual(balance_rows["6001"]["ending_credit"], "0.00")
        # Zero-net accounts: posting keeps normal_side at 0.00, the trial
        # balance reports 0.00 in BOTH ending columns.
        zero = make_voucher("V-0", "2024-05-10", [
            make_entry("6601", "借", debit="7.00"),
            make_entry("6601", "贷", credit="7.00"),
        ])
        pz = post_vouchers(BASE, make_chart(), [zero])
        tz = build_trial_balance(BASE, make_chart(), [zero])
        prow = {r["code"]: r for r in pz["accounts"]}["6601"]
        trow = {r["code"]: r for r in tz["accounts"]}["6601"]
        self.assertEqual((prow["ending_side"], prow["ending_balance"]),
                         ("debit", "0.00"))
        self.assertEqual((trow["ending_debit"], trow["ending_credit"]),
                         ("0.00", "0.00"))

    def test_the_two_results_share_no_mutable_container(self):
        self.assertTrue(
            mutable_ids(self.posted).isdisjoint(mutable_ids(self.balanced)))

    def test_turnover_strings_match_between_entries_row_by_row(self):
        posted_rows = {r["code"]: r for r in self.posted["accounts"]}
        for code, trow in {r["code"]: r
                           for r in self.balanced["accounts"]}.items():
            self.assertEqual(posted_rows[code]["debit_turnover"],
                             trow["debit_turnover"])
            self.assertEqual(posted_rows[code]["credit_turnover"],
                             trow["credit_turnover"])

    def test_mutating_one_public_result_cannot_reach_the_other(self):
        self.posted["accounts"][0]["ending_balance"] = "corrupted"
        self.posted["accounts"].clear()
        self.balanced["totals"]["ending_debit"] = "corrupted"
        again_posted = post_vouchers(BASE, make_chart(), make_batch())
        again_balanced = build_trial_balance(BASE, make_chart(), make_batch())
        self.assertEqual(len(again_posted["accounts"]), 6)
        self.assertNotEqual(
            again_posted["accounts"][0]["ending_balance"], "corrupted")
        self.assertNotEqual(
            again_balanced["totals"]["ending_debit"], "corrupted")


# --------------------------------------------------------------------- #
# Boundary cases through both public entries and the shared fact.
# --------------------------------------------------------------------- #
class SharedFactBoundaryCasesTests(unittest.TestCase):
    def test_empty_batch_rows_and_json_match_between_entries(self):
        posted = post_vouchers(BASE, make_chart(), [])
        balanced = build_trial_balance(BASE, make_chart(), [])
        chart, validated = validated_fixture(make_chart(), [])
        fact = ledger.accumulate_turnovers(chart, validated)
        self.assertEqual(posted["journal"], [])
        self.assertEqual(len(fact), 6)
        self.assertEqual(len(posted["accounts"]), 6)
        self.assertEqual(len(balanced["accounts"]), 6)
        for prow, trow, fact_row in zip(posted["accounts"],
                                        balanced["accounts"], fact):
            self.assertEqual(prow["code"], fact_row.account.code)
            self.assertEqual(trow["code"], fact_row.account.code)
            self.assertEqual(prow["debit_turnover"], "0.00")
            self.assertEqual(trow["credit_turnover"], "0.00")
        self.assertEqual(balanced["totals"]["debit_turnover"], "0.00")
        self.assertEqual(balanced["totals"]["credit_turnover"], "0.00")
        self.assertEqual(balanced["totals"]["ending_debit"], "0.00")
        self.assertEqual(balanced["totals"]["ending_credit"], "0.00")
        self.assertIs(balanced["totals"]["turnover_balanced"], True)
        self.assertIs(balanced["totals"]["ending_balanced"], True)
        # Both documents round-trip and serialize deterministically.
        self.assertEqual(json.loads(json.dumps(posted)), posted)
        self.assertEqual(json.loads(json.dumps(balanced)), balanced)

    def test_empty_chart_empty_batch(self):
        self.assertEqual(
            ledger.accumulate_turnovers(
                *validated_fixture([], [])), ())
        posted = post_vouchers(BASE, [], [])
        balanced = build_trial_balance(BASE, [], [])
        self.assertEqual((posted["journal"], posted["accounts"]), ([], []))
        self.assertEqual(balanced["accounts"], [])
        self.assertEqual(balanced["totals"]["turnover_balanced"], True)
        self.assertEqual(balanced["totals"]["ending_balanced"], True)

    def test_huge_amounts_agree_exactly_across_both_entries(self):
        batch = [make_voucher("V-1", "2024-05-10", [
            make_entry("6601", "大额", debit=HUGE),
            make_entry("2202", "大额", credit=HUGE),
        ])]
        posted = post_vouchers(BASE, make_chart(), batch)
        balanced = build_trial_balance(BASE, make_chart(), batch)
        prow = {r["code"]: r for r in posted["accounts"]}
        trow = {r["code"]: r for r in balanced["accounts"]}
        for code in ("6601", "2202"):
            self.assertEqual(prow[code]["debit_turnover"],
                             trow[code]["debit_turnover"])
            self.assertEqual(prow[code]["credit_turnover"],
                             trow[code]["credit_turnover"])
        self.assertEqual(prow["6601"]["ending_balance"], HUGE)
        self.assertEqual(trow["6601"]["ending_debit"], HUGE)
        self.assertEqual(trow["2202"]["ending_credit"], HUGE)
        self.assertEqual(balanced["totals"]["debit_turnover"], HUGE)
        self.assertEqual(balanced["totals"]["ending_balanced"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
