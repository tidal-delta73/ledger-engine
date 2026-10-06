"""Shared-aggregation regression tests for posting and the trial balance.

``post_vouchers`` and ``build_trial_balance`` must derive their
per-account debit/credit turnovers from one internal accumulation fact
while keeping their distinct public shapes.  These tests lock that
boundary through the public API only:

* both entries report identical ``debit_turnover``/``credit_turnover``
  strings row by row, in the same chart order, on every fixture --
  including empty batches, empty charts, zero-net accounts and amounts
  beyond the float safe-integer range;
* each entry's ending columns are exactly the public derivation of the
  same integer net (normal-side balance for posting, debit-minus-credit
  split for the trial balance), and the totals row sums the same
  turnovers;
* the two results never share a mutable container, and corrupting one
  entry's result cannot reach a later call of the other entry;
* presentation rules stay on their own side: posting rows never grow
  trial-balance columns and vice versa.

Only the public package surface and the standard library are used, so
the suite runs on a plain CPython 3.10+ install.
"""
from __future__ import annotations

import copy
import json
import unittest

from ledger_engine import build_trial_balance, post_vouchers

BASE = "CNY"

# Beyond 2**53 (9007199254740992): float arithmetic would already lose
# the trailing cent here; integer-cents handling must keep it exactly.
HUGE = "9007199254740993.00"

JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}


# --------------------------------------------------------------------- #
# Fixture factories -- fresh containers on every call.
# --------------------------------------------------------------------- #
def make_chart():
    """Both normal sides, an unused active and an inactive account."""
    return [
        {"code": "1001", "name": "库存现金", "normal_side": "debit",
         "active": True},
        {"code": "2202", "name": "应付账款", "normal_side": "credit",
         "active": True},
        {"code": "6601", "name": "管理费用", "normal_side": "debit",
         "active": True},
        {"code": "6001", "name": "主营业务收入", "normal_side": "credit",
         "active": True},
        {"code": "1221", "name": "备用金", "normal_side": "debit",
         "active": True},
        {"code": "1901", "name": "未使用科目", "normal_side": "debit",
         "active": True},
        {"code": "4001", "name": "停用科目", "normal_side": "credit",
         "active": False},
    ]


def make_entry(account_code, summary, debit="0.00", credit="0.00"):
    return {"account_code": account_code, "summary": summary,
            "debit": debit, "credit": credit}


def make_voucher(voucher_id, date_text, entries, currency=BASE):
    return {"voucher_id": voucher_id, "date": date_text,
            "currency": currency, "entries": entries}


def make_batch():
    """Balanced vouchers touching every aggregation edge at once.

    * a huge amount posted on both accounts' normal sides;
    * a credit-normal account posted on its debit side and vice versa;
    * an account whose occurrences net exactly to zero;
    * an account whose net flips against its normal side.
    """
    return [
        make_voucher("V-2024-001", "2024-05-10", [
            make_entry("6601", "大额费用确认", debit=HUGE),
            make_entry("2202", "大额挂账", credit=HUGE),
        ]),
        make_voucher("V-2024-002", "2024-05-11", [
            make_entry("2202", "偿还欠款", debit="123.45"),
            make_entry("1001", "现金支付", credit="123.45"),
        ]),
        make_voucher("V-2024-003", "2024-05-12", [
            make_entry("1221", "划出备用金", debit="50.00"),
            make_entry("1221", "收回备用金", credit="50.00"),
            make_entry("6001", "确认收入", credit="30.00"),
            make_entry("6001", "红字冲回收入", debit="80.00"),
            make_entry("1001", "补差现金", credit="50.00"),
        ]),
    ]


# --------------------------------------------------------------------- #
# Structural helpers (test utilities only -- no private API is touched).
# --------------------------------------------------------------------- #
def cents(amount_text):
    """Parse a two-decimal amount string back into integer cents."""
    whole, frac = amount_text.split(".")
    return int(whole) * 100 + int(frac)


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


def corrupt_deep(value):
    """Aggressively mutate every reachable mutable container in place."""
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if isinstance(child, (dict, list)):
                corrupt_deep(child)
            else:
                value[key] = "POLLUTED"
        value["__injected_by_test__"] = [1, 2, 3]
    elif isinstance(value, list):
        for child in value:
            corrupt_deep(child)
        if value:
            value.reverse()
        value.append("__injected_by_test__")


def call_pair(chart, batch):
    """Run both ledger entries on the same raw inputs."""
    return (post_vouchers(BASE, chart, batch),
            build_trial_balance(BASE, chart, batch))


# --------------------------------------------------------------------- #
# One accumulation fact, two public shapes.
# --------------------------------------------------------------------- #
class SharedAccumulationTests(unittest.TestCase):
    def setUp(self):
        self.posted, self.trial = call_pair(make_chart(), make_batch())

    def test_account_rows_align_in_chart_order(self):
        posted_codes = [row["code"] for row in self.posted["accounts"]]
        trial_codes = [row["code"] for row in self.trial["accounts"]]
        self.assertEqual(posted_codes, trial_codes)
        self.assertEqual(posted_codes,
                         [row["code"] for row in make_chart()])

    def test_turnovers_are_identical_row_by_row(self):
        for posted_row, trial_row in zip(self.posted["accounts"],
                                         self.trial["accounts"]):
            with self.subTest(code=posted_row["code"]):
                self.assertEqual(posted_row["debit_turnover"],
                                 trial_row["debit_turnover"])
                self.assertEqual(posted_row["credit_turnover"],
                                 trial_row["credit_turnover"])
                # Identity fields come from the same chart snapshot too.
                self.assertEqual(posted_row["name"], trial_row["name"])
                self.assertEqual(posted_row["normal_side"],
                                 trial_row["normal_side"])

    def test_both_endings_derive_from_the_same_integer_net(self):
        for posted_row, trial_row in zip(self.posted["accounts"],
                                         self.trial["accounts"]):
            with self.subTest(code=posted_row["code"]):
                net = (cents(posted_row["debit_turnover"])
                       - cents(posted_row["credit_turnover"]))
                # Trial balance: debit-minus-credit split, both columns
                # zero on a zero net.
                self.assertEqual(cents(trial_row["ending_debit"]),
                                 max(net, 0))
                self.assertEqual(cents(trial_row["ending_credit"]),
                                 max(-net, 0))
                # Posting: the same net read in the normal direction.
                normal = posted_row["normal_side"]
                normal_net = net if normal == "debit" else -net
                expected_side = normal if normal_net >= 0 else {
                    "debit": "credit", "credit": "debit"}[normal]
                self.assertEqual(posted_row["ending_side"], expected_side)
                self.assertEqual(cents(posted_row["ending_balance"]),
                                 abs(normal_net))

    def test_totals_sum_the_shared_turnovers(self):
        totals = self.trial["totals"]
        sum_debit = sum(cents(row["debit_turnover"])
                        for row in self.posted["accounts"])
        sum_credit = sum(cents(row["credit_turnover"])
                         for row in self.posted["accounts"])
        self.assertEqual(cents(totals["debit_turnover"]), sum_debit)
        self.assertEqual(cents(totals["credit_turnover"]), sum_credit)
        sum_end_debit = sum(cents(row["ending_debit"])
                            for row in self.trial["accounts"])
        sum_end_credit = sum(cents(row["ending_credit"])
                             for row in self.trial["accounts"])
        self.assertEqual(cents(totals["ending_debit"]), sum_end_debit)
        self.assertEqual(cents(totals["ending_credit"]), sum_end_credit)
        self.assertIs(totals["turnover_balanced"], sum_debit == sum_credit)
        self.assertIs(totals["ending_balanced"],
                      sum_end_debit == sum_end_credit)

    def test_huge_amounts_stay_exact_in_both_shapes(self):
        posted = {row["code"]: row for row in self.posted["accounts"]}
        trial = {row["code"]: row for row in self.trial["accounts"]}
        self.assertEqual(posted["6601"]["debit_turnover"], HUGE)
        self.assertEqual(trial["6601"]["debit_turnover"], HUGE)
        self.assertEqual(trial["6601"]["ending_debit"], HUGE)
        self.assertEqual(posted["6601"]["ending_balance"], HUGE)
        # 2202 nets HUGE credit against a 123.45 debit repayment.
        self.assertEqual(trial["2202"]["ending_credit"],
                         "9007199254740869.55")
        self.assertEqual(posted["2202"]["ending_balance"],
                         "9007199254740869.55")

    def test_presentation_rules_do_not_cross_the_boundary(self):
        for row in self.posted["accounts"]:
            self.assertEqual(
                list(row),
                ["code", "name", "normal_side", "debit_turnover",
                 "credit_turnover", "ending_side", "ending_balance"],
            )
        for row in self.trial["accounts"]:
            self.assertEqual(
                list(row),
                ["code", "name", "normal_side", "debit_turnover",
                 "credit_turnover", "ending_debit", "ending_credit"],
            )
        self.assertEqual(list(self.trial["totals"]),
                         ["debit_turnover", "credit_turnover",
                          "ending_debit", "ending_credit",
                          "turnover_balanced", "ending_balanced"])
        self.assertEqual(list(self.posted),
                         ["base_currency", "journal", "accounts"])
        self.assertEqual(list(self.trial),
                         ["base_currency", "accounts", "totals"])


# --------------------------------------------------------------------- #
# Degenerate inputs: both entries agree on the same zero fact.
# --------------------------------------------------------------------- #
class DegenerateInputTests(unittest.TestCase):
    def test_empty_batch_reports_zero_rows_identically(self):
        posted, trial = call_pair(make_chart(), [])
        self.assertEqual(posted["journal"], [])
        self.assertEqual([row["code"] for row in posted["accounts"]],
                         [row["code"] for row in trial["accounts"]])
        for posted_row, trial_row in zip(posted["accounts"],
                                         trial["accounts"]):
            self.assertEqual(posted_row["debit_turnover"], "0.00")
            self.assertEqual(posted_row["credit_turnover"], "0.00")
            self.assertEqual(trial_row["debit_turnover"], "0.00")
            self.assertEqual(trial_row["credit_turnover"], "0.00")
            self.assertEqual(trial_row["ending_debit"], "0.00")
            self.assertEqual(trial_row["ending_credit"], "0.00")
            self.assertEqual(posted_row["ending_side"],
                             posted_row["normal_side"])
            self.assertEqual(posted_row["ending_balance"], "0.00")
        self.assertEqual(trial["totals"],
                         {"debit_turnover": "0.00",
                          "credit_turnover": "0.00",
                          "ending_debit": "0.00",
                          "ending_credit": "0.00",
                          "turnover_balanced": True,
                          "ending_balanced": True})

    def test_empty_chart_and_empty_batch(self):
        posted, trial = call_pair([], [])
        self.assertEqual(posted["journal"], [])
        self.assertEqual(posted["accounts"], [])
        self.assertEqual(trial["accounts"], [])
        self.assertEqual(trial["totals"]["debit_turnover"], "0.00")
        self.assertIs(trial["totals"]["turnover_balanced"], True)
        self.assertIs(trial["totals"]["ending_balanced"], True)

    def test_zero_net_account_is_zero_in_both_shapes(self):
        posted, trial = call_pair(make_chart(), make_batch())
        posted_row = {r["code"]: r for r in posted["accounts"]}["1221"]
        trial_row = {r["code"]: r for r in trial["accounts"]}["1221"]
        self.assertEqual(posted_row["debit_turnover"], "50.00")
        self.assertEqual(trial_row["debit_turnover"], "50.00")
        self.assertEqual(posted_row["ending_balance"], "0.00")
        self.assertEqual(posted_row["ending_side"], "debit")
        self.assertEqual(trial_row["ending_debit"], "0.00")
        self.assertEqual(trial_row["ending_credit"], "0.00")


# --------------------------------------------------------------------- #
# Isolation: the shared fact never becomes shared mutable state.
# --------------------------------------------------------------------- #
class CrossEntryIsolationTests(unittest.TestCase):
    def test_results_share_no_mutable_containers(self):
        posted, trial = call_pair(make_chart(), make_batch())
        self.assertTrue(
            mutable_ids(posted).isdisjoint(mutable_ids(trial)),
            "posting and trial-balance results must not share dicts/lists",
        )

    def test_corrupting_one_result_cannot_reach_the_other_entry(self):
        chart, batch = make_chart(), make_batch()
        posted, trial = call_pair(chart, batch)
        posted_snapshot = copy.deepcopy(posted)
        trial_snapshot = copy.deepcopy(trial)
        corrupt_deep(posted)
        corrupt_deep(trial)
        reposted, retrial = call_pair(chart, batch)
        self.assertEqual(reposted, posted_snapshot)
        self.assertEqual(retrial, trial_snapshot)

    def test_serialization_is_character_identical_across_repeated_calls(self):
        for _ in range(3):
            posted, trial = call_pair(make_chart(), make_batch())
            self.assertEqual(json.dumps(posted, **JSON_KW),
                             json.dumps(call_pair(make_chart(),
                                                  make_batch())[0],
                                        **JSON_KW))
            self.assertEqual(json.dumps(trial, **JSON_KW),
                             json.dumps(call_pair(make_chart(),
                                                  make_batch())[1],
                                        **JSON_KW))


if __name__ == "__main__":
    unittest.main(verbosity=2)
