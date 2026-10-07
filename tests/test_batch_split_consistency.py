"""Black-box split/merge/reorder consistency tests for the public API.

The baseline suites pin each public entry point
(``normalize_vouchers`` / ``post_vouchers`` / ``build_trial_balance``)
in isolation and prove that posting and the trial balance share one
accumulation fact.  This module instead exercises *ledger-level
invariants* of the three entries together, purely through the public
package surface and the standard library:

* a batch cut into sub-batches along several different boundaries can be
  merged back in integer cents (the integer corresponding to the decimal
  amount strings -- no float is ever involved) and the merged per-account
  turnovers equal the full-batch ``post_vouchers`` and
  ``build_trial_balance`` turnovers, account by account, and the trial
  totals reconcile as well;
* every normalized voucher line corresponds exactly (same voucher id and
  line number) to exactly one journal row, including vouchers that touch
  one account repeatedly;
* reordering vouchers changes only the voucher order of the normalized
  list and journal; account rows stay in chart order and every amount,
  ending side/balance and trial total is unchanged;
* the same equivalent inputs, rebuilt as brand-new containers, serialize
  character-identically under one fixed JSON parameter set from every
  entry point, and no two calls -- across or within entries -- share a
  mutable container;
* deeply corrupting a result never reaches a later call of any entry,
  and the raw chart/vouchers stay byte-for-byte as supplied;
* a bad shard (malformed shape, duplicate id, foreign currency, unknown
  or inactive account, illegal amount, unbalanced voucher) is rejected
  by all three entries with the same existing leaf exception type and
  message, and yields nothing that could be merged.

The success fixture covers, together: an empty batch, one account
debited/credited repeatedly across vouchers and netting to zero,
credit-normal and debit-normal accounts ending on their opposite side,
an unused active account, an inactive unreferenced account and amounts
beyond the float safe-integer range (``2**53 + 1``) combined with
fractional cents.

No private module, intermediate object or implementation detail is
imported or inspected; the version, CLI, exception hierarchy and result
shapes are not modified.  Runs on CPython 3.10+.
"""
from __future__ import annotations

import copy
import json
import unittest

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
    build_trial_balance,
    normalize_vouchers,
    post_vouchers,
)

BASE = "CNY"
ENTRY_POINTS = (normalize_vouchers, post_vouchers, build_trial_balance)

# Fixed serialization parameters shared with the baseline suites.
JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}

# Beyond 2**53 (9007199254740992): a float already drops the trailing
# unit; integer-cents handling must keep the 0.30 carry below exact.
HUGE = "9007199254740993.00"


# --------------------------------------------------------------------- #
# Fixture factories -- fresh containers on every call.
# --------------------------------------------------------------------- #
def make_chart():
    """Seven accounts: both normal sides, an unused and an inactive one."""
    return [
        {"active": True, "code": "1001", "name": "库存现金",
         "normal_side": "debit"},
        {"code": "2202", "name": "应付账款", "normal_side": "credit",
         "active": True},
        {"name": "管理费用", "normal_side": "debit", "code": "6601",
         "active": True},
        {"active": True, "normal_side": "credit", "code": "6001",
         "name": "主营业务收入"},
        {"normal_side": "debit", "active": True, "name": "备用金",
         "code": "1221"},
        {"code": "1901", "name": "未使用科目", "active": True,
         "normal_side": "debit"},
        {"normal_side": "credit", "active": False, "code": "4001",
         "name": "停用但未引用科目"},
    ]


def make_entry(account_code, summary, debit="0.00", credit="0.00"):
    # Insertion order deliberately differs from the public result order.
    return {"credit": credit, "summary": summary,
            "account_code": account_code, "debit": debit}


def make_voucher(voucher_id, date_text, entries, currency=BASE):
    return {"date": date_text, "entries": entries,
            "voucher_id": voucher_id, "currency": currency}


def make_batch():
    """Six balanced vouchers that together hit every merge edge.

    * V1 carries the huge amount on normal sides;
    * V2 repays part of the liability on its debit side and credits cash
      (a debit-normal account used backwards);
    * V3 nets 备用金 to zero, posts income (credit-normal) on the debit
      side hard enough to flip it, and is itself multi-entry balanced;
    * V4 adds fractional cents (0.30) to the huge-expense account so the
      merged turnover must carry a fraction past the float-safe range;
    * V5 swings 备用金 again and builds a balanced entry out of one
      debit line over three credit lines;
    * V6 swings 备用金 back so the *cross-voucher* net on that account is
      exactly zero (100.00 both sides overall), while cash is touched on
      both sides within one voucher.
    """
    return [
        make_voucher("V-2024-001", "2024-05-10", [
            make_entry("6601", "大额费用确认", debit=HUGE),
            make_entry("2202", "大额挂账", credit=HUGE),
        ]),
        make_voucher("V-2024-002", "2024-05-11", [
            make_entry("2202", "偿还欠款(借方发生)", debit="123.45"),
            make_entry("1001", "现金支付", credit="123.45"),
        ]),
        make_voucher("V-2024-003", "2024-05-12", [
            make_entry("1221", "划出备用金", debit="50.00"),
            make_entry("1221", "收回备用金", credit="50.00"),
            make_entry("6001", "确认收入", credit="30.00"),
            make_entry("6001", "红字冲回收入", debit="80.00"),
            make_entry("1001", "补差现金", credit="50.00"),
        ]),
        make_voucher("V-2024-004", "2024-05-13", [
            make_entry("6601", "大额零星费用", debit="0.30"),
            make_entry("2202", "大额零星挂账", credit="0.30"),
        ]),
        make_voucher("V-2024-005", "2024-05-14", [
            make_entry("1221", "再划备用金", debit="50.00"),
            make_entry("1001", "收现", debit="30.00"),
            make_entry("6001", "再冲收入", debit="80.00"),
            make_entry("2202", "对方一", credit="100.00"),
            make_entry("2202", "对方二", credit="60.00"),
        ]),
        make_voucher("V-2024-006", "2024-05-15", [
            make_entry("1221", "再收备用金", credit="50.00"),
            make_entry("1001", "现金入库", debit="50.00"),
        ]),
    ]


# Several genuinely different boundaries: singletons, prefix/suffix,
# unequal chunks, and the trivial single-shard partition.
SPLITS = (
    [(0, 6)],
    [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6)],
    [(0, 3), (3, 6)],
    [(0, 2), (2, 5), (5, 6)],
    [(0, 4), (4, 6)],
)


# --------------------------------------------------------------------- #
# Integer-cents helpers -- the test's own reference arithmetic.
# --------------------------------------------------------------------- #
def to_cents(amount_text):
    """The integer cents denoted by a fixed-point decimal string.

    Pure string handling: ``Decimal``/``float`` are deliberately not used,
    mirroring the no-float contract under test.
    """
    whole, _, frac = amount_text.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2]) if frac else int(whole) * 100


def from_cents(value):
    """Inverse of :func:`to_cents` with exactly two decimal places."""
    return f"{value // 100}.{value % 100:02d}"


def mutable_ids(value):
    """Ids of every dict/list reachable in ``value``."""
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
    """Mutate every reachable mutable container as aggressively as possible."""
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


def posted_rows(result):
    return {row["code"]: row for row in result["accounts"]}


def trial_rows(result):
    return {row["code"]: row for row in result["accounts"]}


# --------------------------------------------------------------------- #
# Sub-batch splitting and integer-cents merging.
# --------------------------------------------------------------------- #
class SubBatchMergeTests(unittest.TestCase):
    def setUp(self):
        self.chart = make_chart()
        self.batch = make_batch()
        self.codes = [a["code"] for a in self.chart]
        self.full_post = post_vouchers(BASE, self.chart, self.batch)
        self.full_trial = build_trial_balance(BASE, self.chart, self.batch)
        self.full_norm = normalize_vouchers(BASE, self.chart, self.batch)

    def _shard_inputs(self, start, stop):
        # Fresh chart snapshot and an independently copied shard, so a
        # shard run can never alias the full run's containers.
        return copy.deepcopy(self.chart), copy.deepcopy(self.batch[start:stop])

    def test_merged_turnovers_equal_full_batch_for_every_split_boundary(self):
        for boundaries in SPLITS:
            with self.subTest(boundaries=boundaries):
                # The boundaries partition the whole batch exactly once.
                covered = [i for start, stop in boundaries
                           for i in range(start, stop)]
                self.assertEqual(covered, list(range(len(self.batch))))
                shard_trial = []
                merged = {code: [0, 0] for code in self.codes}
                for start, stop in boundaries:
                    chart, shard = self._shard_inputs(start, stop)
                    posted = post_vouchers(BASE, chart, shard)
                    trial = build_trial_balance(BASE,
                                                copy.deepcopy(chart),
                                                copy.deepcopy(shard))
                    shard_trial.append(trial)
                    for row in posted["accounts"]:
                        merged[row["code"]][0] += to_cents(
                            row["debit_turnover"])
                        merged[row["code"]][1] += to_cents(
                            row["credit_turnover"])

                # Every chart account is present in the merge and in the
                # full results, full rows in chart order.
                self.assertEqual(set(merged), set(self.codes))
                for code in self.codes:
                    full_p = posted_rows(self.full_post)[code]
                    full_t = trial_rows(self.full_trial)[code]
                    merged_d, merged_c = merged[code]
                    with self.subTest(code=code, boundaries=boundaries):
                        # Merge equals post_vouchers...
                        self.assertEqual(merged_d,
                                         to_cents(full_p["debit_turnover"]))
                        self.assertEqual(merged_c,
                                         to_cents(full_p["credit_turnover"]))
                        # ...and build_trial_balance, account by account.
                        self.assertEqual(merged_d,
                                         to_cents(full_t["debit_turnover"]))
                        self.assertEqual(merged_c,
                                         to_cents(full_t["credit_turnover"]))
                        # The two full entries agree on the same fact.
                        self.assertEqual(full_p["debit_turnover"],
                                         full_t["debit_turnover"])
                        self.assertEqual(full_p["credit_turnover"],
                                         full_t["credit_turnover"])

                # Merged trial totals equal the full-batch trial totals.
                total_d = sum(v[0] for v in merged.values())
                total_c = sum(v[1] for v in merged.values())
                totals = self.full_trial["totals"]
                self.assertEqual(from_cents(total_d),
                                 totals["debit_turnover"])
                self.assertEqual(from_cents(total_c),
                                 totals["credit_turnover"])
                ending_d = sum(max(d - c, 0) for d, c in merged.values())
                ending_c = sum(max(c - d, 0) for d, c in merged.values())
                self.assertEqual(from_cents(ending_d),
                                 totals["ending_debit"])
                self.assertEqual(from_cents(ending_c),
                                 totals["ending_credit"])
                self.assertIs(totals["turnover_balanced"], True)
                self.assertIs(totals["ending_balanced"], True)

                # Summed shard turnover totals equal the merged totals too:
                # each shard's totals is itself a mergeable turnover fact
                # (ending columns are net-derived and not additive across
                # shards, so they are deliberately not merged here).
                summed_d = summed_c = 0
                for trial in shard_trial:
                    summed_d += to_cents(trial["totals"]["debit_turnover"])
                    summed_c += to_cents(trial["totals"]["credit_turnover"])
                self.assertEqual(summed_d, total_d)
                self.assertEqual(summed_c, total_c)
                self.assertEqual(summed_d, summed_c)

    def test_huge_fractional_turnover_merges_exactly_without_float(self):
        # V1 + V4 land on 6601: HUGE debit plus 0.30, across two shards.
        chart, shard_v1 = self._shard_inputs(0, 1)
        _, shard_v4 = self._shard_inputs(3, 4)
        p1 = post_vouchers(BASE, chart, shard_v1)
        p4 = post_vouchers(BASE, copy.deepcopy(self.chart), shard_v4)
        merged = {
            code: to_cents(posted_rows(p1)[code]["debit_turnover"])
            + to_cents(posted_rows(p4)[code]["debit_turnover"])
            for code in self.codes
        }
        full = posted_rows(self.full_post)
        self.assertEqual(from_cents(merged["6601"]),
                         "9007199254740993.30")
        self.assertEqual(full["6601"]["debit_turnover"],
                         "9007199254740993.30")
        self.assertEqual(full["6601"]["ending_balance"],
                         "9007199254740993.30")
        # The credit-normal liability nets the huge credit against the
        # 123.45 cross-voucher debit, keeping full precision.
        self.assertEqual(full["2202"]["ending_balance"],
                         "9007199254741029.85")
        trial = trial_rows(self.full_trial)
        self.assertEqual(trial["2202"]["ending_credit"],
                         "9007199254741029.85")
        # Explicit no-float guard: reference sums stay exact ints.
        self.assertIsInstance(to_cents(HUGE), int)
        self.assertEqual(to_cents(HUGE) + 30, 900719925474099330)

    def test_cross_voucher_repeated_account_nets_to_zero_in_every_shape(self):
        # 1221 is debited and credited across V3/V5/V6: 100.00 each side.
        posted = posted_rows(self.full_post)["1221"]
        trial = trial_rows(self.full_trial)["1221"]
        self.assertEqual(posted["debit_turnover"], "100.00")
        self.assertEqual(posted["credit_turnover"], "100.00")
        self.assertEqual(posted["ending_side"], "debit")  # keeps normal
        self.assertEqual(posted["ending_balance"], "0.00")
        self.assertEqual(trial["ending_debit"], "0.00")
        self.assertEqual(trial["ending_credit"], "0.00")
        # Splitting at different boundaries must reproduce the same zero:
        # merge per-shard integer nets, then derive the ending from scratch.
        for boundaries in SPLITS:
            net = 0
            for start, stop in boundaries:
                chart, shard = self._shard_inputs(start, stop)
                for row in post_vouchers(BASE, chart, shard)["accounts"]:
                    if row["code"] == "1221":
                        net += (to_cents(row["debit_turnover"])
                                - to_cents(row["credit_turnover"]))
            self.assertEqual(net, 0, boundaries)

    def test_opposite_side_balances_survive_the_merge(self):
        posted = posted_rows(self.full_post)
        trial = trial_rows(self.full_trial)
        # 1001 is debit-normal yet ends net-credit (173.45 C vs 80.00 D).
        self.assertEqual(posted["1001"]["ending_side"], "credit")
        self.assertEqual(posted["1001"]["ending_balance"], "93.45")
        self.assertEqual(trial["1001"]["ending_debit"], "0.00")
        self.assertEqual(trial["1001"]["ending_credit"], "93.45")
        # 6001 is credit-normal yet ends net-debit (160.00 D vs 30.00 C).
        self.assertEqual(posted["6001"]["ending_side"], "debit")
        self.assertEqual(posted["6001"]["ending_balance"], "130.00")
        self.assertEqual(trial["6001"]["ending_debit"], "130.00")
        self.assertEqual(trial["6001"]["ending_credit"], "0.00")

    def test_unused_and_inactive_accounts_are_zero_in_every_shard(self):
        for start, stop in [(0, 6), (0, 3), (3, 6), (0, 1)]:
            chart, shard = self._shard_inputs(start, stop)
            posted = post_vouchers(BASE, chart, shard)
            trial = build_trial_balance(BASE, copy.deepcopy(chart),
                                        copy.deepcopy(shard))
            for code in ("1901", "4001"):
                p = posted_rows(posted)[code]
                t = trial_rows(trial)[code]
                self.assertEqual(p["debit_turnover"], "0.00")
                self.assertEqual(p["credit_turnover"], "0.00")
                self.assertEqual(p["ending_balance"], "0.00")
                self.assertEqual(t["ending_debit"], "0.00")
                self.assertEqual(t["ending_credit"], "0.00")

    def test_normalized_lines_correspond_exactly_to_journal_rows(self):
        journal = self.full_post["journal"]
        # One journal row per normalized entry, same voucher/line pairs.
        norm_pairs = [
            (voucher["voucher_id"], entry["line_no"])
            for voucher in self.full_norm
            for entry in voucher["entries"]
        ]
        journal_pairs = [
            (row["voucher_id"], row["line_no"]) for row in journal
        ]
        self.assertEqual(norm_pairs, journal_pairs)

        norm_index = {
            (voucher["voucher_id"], entry["line_no"]): entry
            for voucher in self.full_norm
            for entry in voucher["entries"]
        }
        normal_by_code = {a["code"]: a["normal_side"] for a in self.chart}
        for row in journal:
            with self.subTest(voucher=row["voucher_id"], line=row["line_no"]):
                # Successful index lookup is the one-to-one correspondence:
                # the key set equality above already proved every journal
                # row names one existing normalized voucher/line pair and
                # vice versa, with no pair repeated.
                entry = norm_index[(row["voucher_id"], row["line_no"])]
                # Full correspondence over every field the journal carries.
                self.assertEqual(
                    list(row),
                    ["voucher_id", "date", "line_no", "account_code",
                     "account_name", "summary", "debit", "credit"],
                )
                self.assertEqual(row["line_no"], entry["line_no"])
                self.assertEqual(row["account_code"], entry["account_code"])
                self.assertEqual(row["account_name"], entry["account_name"])
                self.assertEqual(row["summary"], entry["summary"])
                self.assertEqual(row["debit"], entry["debit"])
                self.assertEqual(row["credit"], entry["credit"])
                # The normalized line additionally carries normal_side,
                # which the journal omits; it still equals the chart fact.
                self.assertEqual(entry["normal_side"],
                                 normal_by_code[row["account_code"]])

        # The journal also carries the voucher date per row; it must match
        # the normalized voucher header.
        dates = {v["voucher_id"]: v["date"] for v in self.full_norm}
        for row in journal:
            self.assertEqual(row["date"], dates[row["voucher_id"]])

        # Repeated same-account lines stay distinct on both sides.
        v5_lines = [(e["line_no"], e["account_code"], e["summary"])
                    for e in next(v for v in self.full_norm
                                  if v["voucher_id"] == "V-2024-005")
                    ["entries"]]
        self.assertEqual(v5_lines, [
            (1, "1221", "再划备用金"),
            (2, "1001", "收现"),
            (3, "6001", "再冲收入"),
            (4, "2202", "对方一"),
            (5, "2202", "对方二"),
        ])

    def test_journal_concatenation_of_shards_equals_full_journal(self):
        for boundaries in SPLITS:
            with self.subTest(boundaries=boundaries):
                merged_journal = []
                merged_norm = []
                for start, stop in boundaries:
                    chart, shard = self._shard_inputs(start, stop)
                    merged_journal.extend(
                        post_vouchers(BASE, chart, shard)["journal"])
                    merged_norm.extend(
                        normalize_vouchers(
                            BASE, copy.deepcopy(chart),
                            copy.deepcopy(shard)))
                self.assertEqual(
                    [(r["voucher_id"], r["line_no"], r["account_code"],
                      r["debit"], r["credit"]) for r in merged_journal],
                    [(r["voucher_id"], r["line_no"], r["account_code"],
                      r["debit"], r["credit"])
                     for r in self.full_post["journal"]],
                )
                self.assertEqual(
                    [(v["voucher_id"], e["line_no"])
                     for v in merged_norm for e in v["entries"]],
                    [(v["voucher_id"], e["line_no"])
                     for v in self.full_norm for e in v["entries"]],
                )


# --------------------------------------------------------------------- #
# Empty batch: every entry reports the same empty/zero facts.
# --------------------------------------------------------------------- #
class EmptyBatchMergeTests(unittest.TestCase):
    def test_empty_batch_from_all_three_entries(self):
        chart_a, chart_b, chart_c = (make_chart() for _ in range(3))
        normalized = normalize_vouchers(BASE, chart_a, [])
        posted = post_vouchers(BASE, chart_b, [])
        trial = build_trial_balance(BASE, chart_c, [])
        self.assertEqual(normalized, [])
        self.assertEqual(posted["journal"], [])
        codes = [a["code"] for a in chart_a]
        self.assertEqual([r["code"] for r in posted["accounts"]], codes)
        self.assertEqual([r["code"] for r in trial["accounts"]], codes)
        for p, t in zip(posted["accounts"], trial["accounts"]):
            self.assertEqual(p["debit_turnover"], "0.00")
            self.assertEqual(p["credit_turnover"], "0.00")
            self.assertEqual(p["ending_balance"], "0.00")
            self.assertEqual(p["ending_side"], p["normal_side"])
            self.assertEqual(t["ending_debit"], "0.00")
            self.assertEqual(t["ending_credit"], "0.00")
        self.assertEqual(trial["totals"], {
            "debit_turnover": "0.00",
            "credit_turnover": "0.00",
            "ending_debit": "0.00",
            "ending_credit": "0.00",
            "turnover_balanced": True,
            "ending_balanced": True,
        })

    def test_empty_chart_and_empty_batch_merge_is_empty(self):
        posted = post_vouchers(BASE, [], [])
        trial = build_trial_balance(BASE, [], [])
        normalized = normalize_vouchers(BASE, [], [])
        self.assertEqual(posted["accounts"], [])
        self.assertEqual(posted["journal"], [])
        self.assertEqual(trial["accounts"], [])
        self.assertEqual(normalized, [])
        self.assertEqual(trial["totals"]["debit_turnover"], "0.00")
        self.assertIs(trial["totals"]["turnover_balanced"], True)

    def test_merging_all_empty_shards_is_the_zero_fact(self):
        chart = make_chart()
        # Several "shards" that are all empty must merge to the zero fact.
        shards = [post_vouchers(BASE, copy.deepcopy(chart), [])
                  for _ in range(4)]
        merged = {r["code"]: [0, 0] for r in shards[0]["accounts"]}
        for result in shards:
            for row in result["accounts"]:
                merged[row["code"]][0] += to_cents(row["debit_turnover"])
                merged[row["code"]][1] += to_cents(row["credit_turnover"])
        # Every chart account is present with both turnovers at zero.
        self.assertEqual(set(merged), {a["code"] for a in chart})
        for code, (debit, credit) in merged.items():
            self.assertEqual((debit, credit), (0, 0), code)


# --------------------------------------------------------------------- #
# Voucher reordering: only voucher order may change.
# --------------------------------------------------------------------- #
class VoucherReorderTests(unittest.TestCase):
    REORDERS = (
        [5, 4, 3, 2, 1, 0],                       # reversed
        [0, 5, 1, 4, 2, 3],                       # interleaved
        [2, 0, 5, 1, 4, 3],                       # arbitrary permutation
        [3, 0, 1, 2, 4, 5],                       # near-original shuffle
    )

    def setUp(self):
        self.chart = make_chart()
        self.batch = make_batch()

    def _reorder(self, order):
        return [copy.deepcopy(self.batch[i]) for i in order]

    def test_normalized_and_journal_follow_only_the_new_voucher_order(self):
        normalized = normalize_vouchers(BASE, copy.deepcopy(self.chart),
                                        copy.deepcopy(self.batch))
        journal = post_vouchers(BASE, copy.deepcopy(self.chart),
                                copy.deepcopy(self.batch))
        for order in self.REORDERS:
            with self.subTest(order=order):
                self.assertEqual(sorted(order), list(range(len(self.batch))))
                reordered = self._reorder(order)
                norm2 = normalize_vouchers(BASE, make_chart(), reordered)
                post2 = post_vouchers(BASE, make_chart(),
                                      copy.deepcopy(reordered))

                expected_ids = [self.batch[i]["voucher_id"] for i in order]
                self.assertEqual([v["voucher_id"] for v in norm2],
                                 expected_ids)
                # Journal voucher blocks follow the new order; within a
                # block line numbers still run 1..n untouched.
                journal_order = []
                for row in post2["journal"]:
                    if not journal_order or journal_order[-1] != row[
                            "voucher_id"]:
                        journal_order.append(row["voucher_id"])
                self.assertEqual(journal_order, expected_ids)
                by_id = {v["voucher_id"]: v for v in normalized}
                for voucher in norm2:
                    original = by_id[voucher["voucher_id"]]
                    # Same voucher normalized anywhere is the same voucher.
                    self.assertEqual(voucher, original)
                for voucher_id in expected_ids:
                    lines = [r for r in post2["journal"]
                             if r["voucher_id"] == voucher_id]
                    self.assertEqual([r["line_no"] for r in lines],
                                     list(range(1, len(lines) + 1)))
                    original_lines = [
                        r for r in journal["journal"]
                        if r["voucher_id"] == voucher_id
                    ]
                    self.assertEqual(
                        [(r["line_no"], r["account_code"], r["summary"],
                          r["debit"], r["credit"]) for r in lines],
                        [(r["line_no"], r["account_code"], r["summary"],
                          r["debit"], r["credit"]) for r in original_lines],
                    )

    def test_account_rows_amounts_endings_and_totals_are_order_invariant(self):
        posted1 = post_vouchers(BASE, make_chart(), make_batch())
        trial1 = build_trial_balance(BASE, make_chart(), make_batch())
        for order in self.REORDERS:
            with self.subTest(order=order):
                reordered = self._reorder(order)
                posted2 = post_vouchers(BASE, make_chart(), reordered)
                trial2 = build_trial_balance(BASE, make_chart(),
                                             copy.deepcopy(reordered))
                # Account rows stay in chart order, fully value-equal.
                self.assertEqual(posted2["accounts"], posted1["accounts"])
                self.assertEqual(trial2["accounts"], trial1["accounts"])
                self.assertEqual(trial2["totals"], trial1["totals"])

    def test_reorder_is_equivalent_under_split_merge(self):
        # Reordering, then splitting, then merging integer turnovers still
        # equals the original full-batch fact.
        full = post_vouchers(BASE, make_chart(), make_batch())
        order = [2, 0, 5, 1, 4, 3]
        reordered = self._reorder(order)
        merged = {}
        for start, stop in [(0, 2), (2, 4), (4, 6)]:
            chart = make_chart()
            result = post_vouchers(BASE, chart, reordered[start:stop])
            for row in result["accounts"]:
                d = merged.setdefault(row["code"], [0, 0])
                d[0] += to_cents(row["debit_turnover"])
                d[1] += to_cents(row["credit_turnover"])
        for row in full["accounts"]:
            with self.subTest(code=row["code"]):
                self.assertEqual(merged[row["code"]], [
                    to_cents(row["debit_turnover"]),
                    to_cents(row["credit_turnover"]),
                ])


# --------------------------------------------------------------------- #
# Fresh-container, character-level serialization and isolation.
# --------------------------------------------------------------------- #
class FreshContainerAndSerializationTests(unittest.TestCase):
    def test_equivalent_inputs_serialize_identically_from_all_entries(self):
        # Two independent constructions of the same ledger set; within
        # each, every entry gets its own fresh chart and batch.
        texts_a = {}
        texts_b = {}
        for entry_point in ENTRY_POINTS:
            texts_a[entry_point.__name__] = json.dumps(
                entry_point(BASE, make_chart(), make_batch()), **JSON_KW)
        for entry_point in ENTRY_POINTS:
            texts_b[entry_point.__name__] = json.dumps(
                entry_point(BASE, make_chart(), make_batch()), **JSON_KW)
        for entry_point in ENTRY_POINTS:
            name = entry_point.__name__
            self.assertEqual(texts_a[name], texts_b[name])
            # Character-level equality, not just value equality.
            self.assertEqual(
                list(texts_a[name]), list(texts_b[name]),
            )
            self.assertEqual(texts_a[name].encode("utf-8"),
                             texts_b[name].encode("utf-8"))
            # normalize's text must be a JSON array; the two others objects.
            decoded = json.loads(texts_a[name])
            if entry_point is normalize_vouchers:
                self.assertIsInstance(decoded, list)
            else:
                self.assertIsInstance(decoded, dict)

    def test_no_two_calls_share_any_mutable_container(self):
        results = {
            entry_point.__name__: [
                entry_point(BASE, make_chart(), make_batch())
                for _ in range(3)
            ]
            for entry_point in ENTRY_POINTS
        }
        all_id_sets = []
        for name, calls in results.items():
            for result in calls:
                all_id_sets.append(mutable_ids(result))
            # Repeated calls of the same entry are disjoint pairwise.
            for i in range(len(calls)):
                for j in range(i + 1, len(calls)):
                    self.assertTrue(
                        mutable_ids(calls[i]).isdisjoint(
                            mutable_ids(calls[j])),
                        f"{name} calls {i}/{j} share a container",
                    )
        # And the three entries never share containers across entries.
        for i in range(len(all_id_sets)):
            for j in range(i + 1, len(all_id_sets)):
                self.assertTrue(
                    all_id_sets[i].isdisjoint(all_id_sets[j]),
                    "results from different entry points/calls share "
                    "a mutable container",
                )

    def test_results_never_share_containers_with_raw_inputs(self):
        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                chart, batch = make_chart(), make_batch()
                result = entry_point(BASE, chart, batch)
                input_ids = mutable_ids(chart) | mutable_ids(batch)
                self.assertTrue(
                    mutable_ids(result).isdisjoint(input_ids),
                    f"{entry_point.__name__} reused an input container",
                )

    def test_deep_corruption_of_one_result_cannot_reach_later_calls(self):
        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                chart, batch = make_chart(), make_batch()
                first = entry_point(BASE, chart, batch)
                snapshot = copy.deepcopy(first)
                corrupt_deep(first)
                self.assertNotEqual(first, snapshot)
                # Recompute from the same raw objects: must equal the
                # untouched snapshot exactly, character for character.
                again = entry_point(BASE, chart, batch)
                self.assertEqual(again, snapshot)
                self.assertEqual(json.dumps(again, **JSON_KW),
                                 json.dumps(snapshot, **JSON_KW))

    def test_raw_inputs_remain_identical_after_all_calls(self):
        chart, batch = make_chart(), make_batch()
        chart_copy, batch_copy = copy.deepcopy(chart), copy.deepcopy(batch)
        for entry_point in ENTRY_POINTS:
            entry_point(BASE, chart, batch)
        self.assertEqual(chart, chart_copy)
        self.assertEqual(batch, batch_copy)
        # Raw terse/zero spellings survive untouched in the caller objects.
        self.assertEqual(batch[0]["entries"][0]["debit"], HUGE)

    def test_mutated_result_from_one_entry_does_not_reach_another_entry(self):
        chart, batch = make_chart(), make_batch()
        posted = post_vouchers(BASE, chart, batch)
        trial = build_trial_balance(BASE, chart, batch)
        normalized = normalize_vouchers(BASE, chart, batch)
        snapshots = (copy.deepcopy(posted), copy.deepcopy(trial),
                     copy.deepcopy(normalized))
        corrupt_deep(posted)
        corrupt_deep(trial)
        corrupt_deep(normalized)
        again = (post_vouchers(BASE, chart, batch),
                 build_trial_balance(BASE, chart, batch),
                 normalize_vouchers(BASE, chart, batch))
        for fresh, snapshot in zip(again, snapshots):
            self.assertEqual(fresh, snapshot)


# --------------------------------------------------------------------- #
# Bad shards: all three entries fail with the same leaf type *and*
# message, and nothing partial is returned or merged.
# --------------------------------------------------------------------- #
def good_voucher(voucher_id="V-OK", date_text="2024-05-10"):
    return make_voucher(voucher_id, date_text, [
        make_entry("6601", "借", debit="100.00"),
        make_entry("2202", "贷", credit="100.00"),
    ])


def bad_shards():
    """Each case is one partition whose bad shard carries a defect.

    Yields ``(label, expected_exc, full_batch, bad_start, bad_stop)``:
    the batch has exactly one defect, located in the shard slice
    ``batch[bad_start:bad_stop]`` (the shard alone must raise).  The
    vouchers outside that slice form a clean, independently valid batch.
    """
    # 1. malformed structure (only one entry, plus a non-string currency)
    malformed = make_voucher("V-BAD", "2024-05-11",
                             [make_entry("6601", "孤行", debit="1.00")])
    malformed["currency"] = 123
    yield ("malformed", VoucherFormatError,
           [good_voucher("V-1"), malformed, good_voucher("V-3")], 1, 2)

    # 2. duplicate voucher id *within* one shard (a cross-boundary
    #    duplicate is covered separately below)
    yield ("duplicate-in-shard", DuplicateVoucherError,
           [good_voucher("V-1"), good_voucher("V-2"),
            good_voucher("V-2"), good_voucher("V-4")], 1, 3)

    # 3. currency mismatch (foreign currency)
    fx = make_voucher("V-FX", "2024-05-11", [
        make_entry("9999", "未知科目", debit="1.005"),
        make_entry("4001", "停用科目", credit="1.00"),
    ], currency="USD")
    yield ("currency", UnsupportedCurrencyError,
           [good_voucher("V-1"), fx, good_voucher("V-3")], 1, 2)

    # 4. unknown account (also carries a masked inactive/amount defect)
    unknown = make_voucher("V-U", "2024-05-11", [
        make_entry("9999", "未知且金额坏", debit="1.005"),
        make_entry("4001", "停用科目", credit="1.00"),
        make_entry("2202", "刻意不平", credit="9.00"),
    ])
    yield ("unknown-account", UnknownAccountError,
           [good_voucher("V-1"), unknown, good_voucher("V-3")], 1, 2)

    # 5. inactive account (also carries a masked bad amount)
    inactive = make_voucher("V-I", "2024-05-11", [
        make_entry("4001", "停用且金额坏", debit="1.005"),
        make_entry("9999", "未知科目", credit="1.00"),
        make_entry("2202", "刻意不平", credit="9.00"),
    ])
    yield ("inactive-account", InactiveAccountError,
           [good_voucher("V-1"), inactive, good_voucher("V-3")], 1, 2)

    # 6. illegal amount (three decimal places; masks unbalanced total)
    bad_amount = make_voucher("V-A", "2024-05-11", [
        make_entry("6601", "三位小数", debit="1.005"),
        make_entry("2202", "金额不等", credit="9.00"),
    ])
    yield ("invalid-amount", InvalidEntryAmountError,
           [good_voucher("V-1"), bad_amount, good_voucher("V-3")], 1, 2)

    # 7. unbalanced voucher (the last boundary)
    unbalanced = make_voucher("V-N", "2024-05-11", [
        make_entry("6601", "借五元", debit="5.00"),
        make_entry("2202", "贷四元", credit="4.00"),
    ])
    yield ("unbalanced", UnbalancedVoucherError,
           [good_voucher("V-1"), unbalanced, good_voucher("V-3")], 1, 2)


class BadShardTests(unittest.TestCase):
    def assert_all_three_fail_identically(self, expected_exc, chart, batch):
        """All three entries raise the exact leaf type and one message."""
        messages = []
        for entry_point in ENTRY_POINTS:
            chart_arg = copy.deepcopy(chart)
            batch_arg = copy.deepcopy(batch)
            with self.assertRaises(LedgerEngineError) as caught:
                entry_point(BASE, chart_arg, batch_arg)
            self.assertIs(
                type(caught.exception), expected_exc,
                f"{entry_point.__name__} must raise the leaf "
                f"{expected_exc.__name__}, got "
                f"{type(caught.exception).__name__}",
            )
            messages.append(str(caught.exception))
        self.assertEqual(len(set(messages)), 1,
                         f"messages differ across entries: {messages}")
        return messages[0]

    def test_each_bad_shard_is_rejected_identically_by_all_entries(self):
        for label, expected_exc, batch, bad_start, bad_stop in bad_shards():
            with self.subTest(case=label):
                chart = make_chart()
                self.assert_all_three_fail_identically(
                    expected_exc, chart, batch)
                # The defect survives a split: processing the offending
                # shard alone raises, so it can never be merged.
                shard = copy.deepcopy(batch[bad_start:bad_stop])
                self.assert_all_three_fail_identically(
                    expected_exc, make_chart(), shard)

    def test_bad_shard_returns_nothing_partial_to_merge(self):
        for label, _expected_exc, batch, bad_start, bad_stop in bad_shards():
            for entry_point in ENTRY_POINTS:
                with self.subTest(case=label, entry=entry_point.__name__):
                    with self.assertRaises(LedgerEngineError):
                        entry_point(
                            BASE, make_chart(),
                            copy.deepcopy(batch[bad_start:bad_stop]))
            # The vouchers outside the bad shard form a clean, uniquely
            # identified batch and still succeed on their own: only the
            # bad shard is poisonous, proving there is no cross-shard
            # contamination either way.
            clean = (batch[:bad_start] + batch[bad_stop:])
            for entry_point in ENTRY_POINTS:
                result = entry_point(BASE, make_chart(),
                                     copy.deepcopy(clean))
                self.assertIsNotNone(result)

    def test_duplicate_across_split_boundary_is_still_rejected(self):
        # The two identical ids sit in *different* shards: each shard alone
        # is fine, so a naive "validate shards then merge" implementation
        # would launder the duplicate.  The full-batch entries must still
        # reject it identically; and the merged-result approach must never
        # silently accept it here (the defect is observable on the whole).
        batch = [good_voucher("V-DUP", "2024-05-10"),
                 good_voucher("V-OTHER", "2024-05-11"),
                 good_voucher("V-DUP", "2024-05-12")]
        self.assert_all_three_fail_identically(
            DuplicateVoucherError, make_chart(), batch)
        # Each shard by itself is valid -- that is exactly why callers
        # cannot assume shard validity implies whole-batch validity.
        for shard in (batch[:2], batch[1:]):
            normalize_vouchers(BASE, make_chart(), copy.deepcopy(shard))
            post_vouchers(BASE, make_chart(), copy.deepcopy(shard))
            build_trial_balance(BASE, make_chart(), copy.deepcopy(shard))

    def test_chart_level_failure_is_shared_by_all_three_entries(self):
        chart = make_chart()
        chart.append({"code": "1001", "name": "重复科目",
                      "normal_side": "debit", "active": True})
        messages = []
        for entry_point in ENTRY_POINTS:
            with self.assertRaises(ChartOfAccountsError) as caught:
                entry_point(BASE, copy.deepcopy(chart),
                            [good_voucher()])
            self.assertIs(type(caught.exception), ChartOfAccountsError)
            messages.append(str(caught.exception))
        self.assertEqual(len(set(messages)), 1)

    def test_failed_call_leaves_raw_containers_untouched(self):
        for label, expected_exc, batch, bad_start, bad_stop in bad_shards():
            chart = make_chart()
            chart_copy = copy.deepcopy(chart)
            batch_copy = copy.deepcopy(batch)
            for entry_point in ENTRY_POINTS:
                with self.subTest(case=label, entry=entry_point.__name__):
                    with self.assertRaises(LedgerEngineError):
                        entry_point(BASE, chart, batch)
            self.assertEqual(chart, chart_copy)
            self.assertEqual(batch, batch_copy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
