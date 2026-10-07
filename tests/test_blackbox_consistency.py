"""Black-box consistency tests across the three public entry points.

The baseline pins each public entry point --
:func:`ledger_engine.normalize_vouchers`,
:func:`ledger_engine.post_vouchers` and
:func:`ledger_engine.build_trial_balance` -- in isolation.  This module
treats all three as black boxes and locks the *repartitioning* and
*cross-entry reconciliation* contract: the same ledger set must tell the
same summary story however its balanced vouchers are split, merged or
reordered, as long as the accounting meaning is unchanged.

Concretely it asserts, through the public package surface plus the
standard library only:

* take the full-batch result, then cut the same vouchers into several
  sub-batches along different boundaries and process every shard with
  every entry point; merging the shards' integer-cent amounts (parsed
  from the decimal strings, never via ``float``) reproduces the full
  batch's per-account ``post_vouchers`` and ``build_trial_balance``
  turnovers and the trial-balance totals exactly, including amounts
  beyond the float safe-integer range;
* every row emitted by ``normalize_vouchers`` corresponds one-to-one,
  voucher id and line number included, to the ``post_vouchers`` journal;
* reordering vouchers only permutes the normalization result and the
  journal along the new voucher order; account rows stay in chart order
  and no turnover, ending direction, ending balance or trial total may
  change;
* scenarios cover the empty batch, one account repeatedly debited and
  credited across vouchers until it nets to zero, balances landing on
  the side opposite to ``normal_side``, unused active accounts and
  unreferenced inactive accounts;
* equivalent inputs rebuilt as brand-new containers yield character
  identical output under fixed JSON serialization parameters for all
  three entry points, no two calls (and no two entries on the same
  inputs) share a mutable container, and deeply corrupting one result
  never reaches a later call while the raw chart and batch stay
  byte-for-byte the same supplied objects;
* a malformed shard (bad shape, duplicate voucher id inside the shard,
  foreign currency, unknown or inactive account, illegal amount,
  unbalanced voucher) makes all three entry points raise the existing
  leaf exception with the existing validation order and message; no
  result is returned and the bad shard never participates in a merge.

No private intermediate object is read, no version number, CLI surface,
exception hierarchy or returned field is changed.  Runs on CPython
3.10+.
"""
from __future__ import annotations

import copy
import itertools
import json
import unittest

from ledger_engine import (
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

# Beyond 2**53 (9007199254740992): converting this to float would lose
# the trailing cent, so any accidental float path breaks these fixtures.
HUGE = "9007199254740993.00"

# Pinned serialization parameters, shared with the baseline suites.
JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}

ENTRY_POINTS = (normalize_vouchers, post_vouchers, build_trial_balance)


# --------------------------------------------------------------------- #
# Fixture factories -- fresh containers on every call.
# --------------------------------------------------------------------- #
def make_chart():
    """Seven accounts: both normal sides, an unused and an inactive one.

    Key insertion order is deliberately scrambled relative to list order
    so output ordering can never be mistaken for input dict order.
    """
    return [
        {"active": True, "code": "1001", "name": "库存现金",
         "normal_side": "debit"},
        {"normal_side": "credit", "name": "应付账款", "active": True,
         "code": "2202"},
        {"code": "6601", "normal_side": "debit", "name": "管理费用",
         "active": True},
        {"name": "主营业务收入", "active": True, "code": "6001",
         "normal_side": "credit"},
        {"name": "备用金", "code": "1221", "normal_side": "debit",
         "active": True},
        {"active": True, "normal_side": "debit", "code": "1901",
         "name": "未使用科目"},
        {"code": "4001", "active": False, "name": "停用但未引用科目",
         "normal_side": "credit"},
    ]


def make_entry(account_code, summary, debit="0.00", credit="0.00"):
    # Key order deliberately varies entry to entry.
    return {"summary": summary, "credit": credit,
            "account_code": account_code, "debit": debit}


def make_voucher(voucher_id, date_text, entries, currency=BASE):
    return {"entries": entries, "currency": currency,
            "voucher_id": voucher_id, "date": date_text}


def make_batch():
    """Four balanced vouchers covering every required scenario together.

    * V1 moves a huge amount on both accounts' normal sides;
    * V2 repays part of the liability on its *debit* side, paying cash
      (debit-normal) from the *credit* side;
    * V3 debits and credits 备用金 across two same-account entries so the
      account's own occurrences net to zero, while income
      (credit-normal) is pushed to its debit side hard enough to flip
      inside that voucher;
    * V4 repeats the same-account zeroing pair, then brings in a little
      cash against a little income -- small enough that income
      (credit-normal) still ends on its debit side, while cash
      (debit-normal) still ends on its credit side; balanced overall.

    Both normal directions therefore keep at least one account whose
    ending balance sits on the opposite side.  The unused account 1901
    and the inactive account 4001 are never referenced by any voucher.
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
        make_voucher(
            "V-2024-004", "2024-05-13",
            [
                make_entry("1221", "再领备用金", debit="20.00"),
                make_entry("1221", "核销备用金", credit="20.00"),
                make_entry("1001", "销售收款", debit="40.00"),
                make_entry("6001", "再确认收入", credit="40.00"),
            ],
        ),
    ]


def make_zeroing_batch():
    """A tiny batch where 1001 oscillates and nets to zero overall.

    Across three vouchers cash is debited then credited, debited then
    credited again, so both turnovers are 60.00 and the net is zero.
    """
    return [
        make_voucher("Z-001", "2024-06-01", [
            make_entry("1001", "收款", debit="40.00"),
            make_entry("6001", "挂收入", credit="40.00"),
        ]),
        make_voucher("Z-002", "2024-06-02", [
            make_entry("6601", "报销费用", debit="20.00"),
            make_entry("1001", "付款", credit="40.00"),
            make_entry("1001", "同日补回", debit="20.00"),
        ]),
        make_voucher("Z-003", "2024-06-03", [
            make_entry("6601", "又一笔费用", debit="20.00"),
            make_entry("1001", "再付款", credit="20.00"),
        ]),
    ]


# --------------------------------------------------------------------- #
# Integer-cents helpers -- reference arithmetic never touches a float.
# --------------------------------------------------------------------- #
def to_cents(amount_text):
    """Parse a fixed-point amount string into integer cents exactly."""
    whole, frac = amount_text.split(".")
    return int(whole) * 100 + int(frac)


def from_cents(cents_value):
    """Render integer cents with exactly two decimal places (reference)."""
    return f"{cents_value // 100}.{cents_value % 100:02d}"


def partitions_of(index_list):
    """All ordered list partitions of ``index_list`` into non-empty parts.

    Each cut position between positions is either cut or not; every
    boundary shape (single full batch, one-voucher shards and everything
    in between) is covered.
    """
    if not index_list:
        yield []
        return
    cuts = len(index_list) - 1
    for mask in range(1 << cuts):
        parts, current = [], [index_list[0]]
        for position in range(cuts):
            if mask & (1 << position):
                parts.append(current)
                current = [index_list[position + 1]]
            else:
                current.append(index_list[position + 1])
        parts.append(current)
        yield parts


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


def call_all(chart, batch):
    """Invoke all three entry points on the same raw inputs."""
    return (
        normalize_vouchers(BASE, chart, batch),
        post_vouchers(BASE, chart, batch),
        build_trial_balance(BASE, chart, batch),
    )


def posted_rows_by_code(posted):
    return {row["code"]: row for row in posted["accounts"]}


def trial_rows_by_code(trial):
    return {row["code"]: row for row in trial["accounts"]}


# --------------------------------------------------------------------- #
# Split / merge: sharded processing reproduces the full-batch facts.
# --------------------------------------------------------------------- #
class SplitMergeConsistencyTests(unittest.TestCase):
    def run_shard_checks(self, chart, batch):
        """Full-batch facts versus every partition of the voucher list."""
        chart_codes = [account["code"] for account in chart]

        normalized_full = normalize_vouchers(BASE, chart, batch)
        posted_full = post_vouchers(BASE, chart, batch)
        trial_full = build_trial_balance(BASE, chart, batch)

        full_posted = posted_rows_by_code(posted_full)
        full_trial = trial_rows_by_code(trial_full)
        full_posted_turnovers = {
            code: (to_cents(full_posted[code]["debit_turnover"]),
                   to_cents(full_posted[code]["credit_turnover"]))
            for code in chart_codes
        }
        full_trial_totals = tuple(
            to_cents(trial_full["totals"][field])
            for field in ("debit_turnover", "credit_turnover",
                          "ending_debit", "ending_credit")
        )

        index_order = list(range(len(batch)))
        partition_count = 0
        for partition in partitions_of(index_order):
            partition_count += 1
            merged = {code: [0, 0] for code in chart_codes}
            merged_journal = []
            merged_normalized = []
            for part in partition:
                shard = [batch[i] for i in part]
                # Every entry point must succeed independently per shard.
                normalized_shard = normalize_vouchers(
                    BASE, copy.deepcopy(chart), copy.deepcopy(shard))
                posted_shard = post_vouchers(
                    BASE, copy.deepcopy(chart), copy.deepcopy(shard))
                trial_shard = build_trial_balance(
                    BASE, copy.deepcopy(chart), copy.deepcopy(shard))

                shard_posted = posted_rows_by_code(posted_shard)
                shard_trial = trial_rows_by_code(trial_shard)
                # The two accumulating entries agree on each shard too.
                for code in chart_codes:
                    self.assertEqual(
                        shard_posted[code]["debit_turnover"],
                        shard_trial[code]["debit_turnover"])
                    self.assertEqual(
                        shard_posted[code]["credit_turnover"],
                        shard_trial[code]["credit_turnover"])
                    merged[code][0] += to_cents(
                        shard_posted[code]["debit_turnover"])
                    merged[code][1] += to_cents(
                        shard_posted[code]["credit_turnover"])
                # Merged reference facts must be integers, never floats.
                self.assertIsInstance(merged[code][0], int)
                self.assertIsInstance(merged[code][1], int)

                merged_journal.extend(posted_shard["journal"])
                merged_normalized.extend(normalized_shard)

                # Shard chart rows stay in chart order, zero-padded.
                self.assertEqual(
                    [row["code"] for row in posted_shard["accounts"]],
                    chart_codes)
                self.assertEqual(
                    [row["code"] for row in trial_shard["accounts"]],
                    chart_codes)

            # Merged integer-cent turnovers equal the full batch, account
            # by account, and render back to the exact full-batch strings.
            for code in chart_codes:
                merged_debit, merged_credit = merged[code]
                full_debit, full_credit = full_posted_turnovers[code]
                self.assertEqual(merged_debit, full_debit,
                                 f"shard-merged debit for {code}")
                self.assertEqual(merged_credit, full_credit,
                                 f"shard-merged credit for {code}")
                self.assertEqual(
                    from_cents(merged_debit),
                    full_posted[code]["debit_turnover"])
                self.assertEqual(
                    from_cents(merged_credit),
                    full_trial[code]["credit_turnover"])

            # Merged per-account turnovers imply the same trial totals as
            # the full batch -- totals are sums over the same chart rows.
            merged_totals = [0, 0, 0, 0]
            for code in chart_codes:
                debit, credit = merged[code]
                merged_totals[0] += debit
                merged_totals[1] += credit
                net = debit - credit
                if net > 0:
                    merged_totals[2] += net
                elif net < 0:
                    merged_totals[3] += -net
            self.assertEqual(tuple(merged_totals), full_trial_totals)
            self.assertEqual(
                [from_cents(value) for value in merged_totals],
                [trial_full["totals"][field]
                 for field in ("debit_turnover", "credit_turnover",
                               "ending_debit", "ending_credit")])
            self.assertTrue(trial_full["totals"]["turnover_balanced"])
            self.assertTrue(trial_full["totals"]["ending_balanced"])

            # Journal/normalization merged over contiguous shards that
            # follow batch order reproduce the full documents exactly.
            self.assertEqual(merged_journal, posted_full["journal"])
            self.assertEqual(merged_normalized, normalized_full)

        # Sanity: a non-trivial partition set was actually exercised.
        self.assertEqual(partition_count, 1 << max(0, len(batch) - 1))

    def test_rich_fixture_every_cut_boundary(self):
        self.run_shard_checks(make_chart(), make_batch())

    def test_zeroing_fixture_every_cut_boundary(self):
        self.run_shard_checks(make_chart(), make_zeroing_batch())

    def test_empty_batch_merges_as_all_zero_facts(self):
        chart = make_chart()
        chart_codes = [account["code"] for account in chart]
        normalized, posted, trial = call_all(chart, [])
        self.assertEqual(normalized, [])
        self.assertEqual(posted["journal"], [])

        # The empty batch has exactly one partition: the empty list of
        # shards.  Merging zero shards contributes (0, 0) per account and
        # must reconcile with the full (empty) batch's zero facts.
        self.assertEqual(list(partitions_of([])), [[]])
        merged = {code: [0, 0] for code in chart_codes}
        posted_rows = posted_rows_by_code(posted)
        trial_rows = trial_rows_by_code(trial)
        for code in chart_codes:
            merged_debit, merged_credit = merged[code]
            # Reference arithmetic stays in integer cents, never float.
            self.assertIsInstance(merged_debit, int)
            self.assertIsInstance(merged_credit, int)
            self.assertEqual(merged_debit,
                             to_cents(posted_rows[code]["debit_turnover"]))
            self.assertEqual(merged_credit,
                             to_cents(posted_rows[code]["credit_turnover"]))
            self.assertEqual(merged_debit,
                             to_cents(trial_rows[code]["debit_turnover"]))
            self.assertEqual(merged_credit,
                             to_cents(trial_rows[code]["credit_turnover"]))
            self.assertEqual(posted_rows[code]["ending_balance"], "0.00")
        self.assertEqual(
            trial["totals"],
            {"debit_turnover": "0.00", "credit_turnover": "0.00",
             "ending_debit": "0.00", "ending_credit": "0.00",
             "turnover_balanced": True, "ending_balanced": True})

    def test_singleton_shards_merge_back_to_the_full_batch(self):
        # The most aggressive split: each voucher processed alone.
        chart, batch = make_chart(), make_batch()
        posted_full = post_vouchers(BASE, chart, batch)
        trial_full = build_trial_balance(BASE, chart, batch)
        merged = {row["code"]: [0, 0] for row in posted_full["accounts"]}
        for voucher in batch:
            posted_shard = post_vouchers(BASE, copy.deepcopy(chart),
                                         copy.deepcopy([voucher]))
            for row in posted_shard["accounts"]:
                merged[row["code"]][0] += to_cents(row["debit_turnover"])
                merged[row["code"]][1] += to_cents(row["credit_turnover"])
        for row in posted_full["accounts"]:
            self.assertEqual(from_cents(merged[row["code"]][0]),
                             row["debit_turnover"])
            self.assertEqual(from_cents(merged[row["code"]][1]),
                             row["credit_turnover"])
        for row in trial_full["accounts"]:
            self.assertEqual(from_cents(merged[row["code"]][0]),
                             row["debit_turnover"])
            self.assertEqual(from_cents(merged[row["code"]][1]),
                             row["credit_turnover"])

    def test_huge_amounts_never_pass_through_float(self):
        # The merged cents carry the exact trailing cent that float
        # arithmetic at this magnitude would drop.
        chart, batch = make_chart(), make_batch()
        posted_full = post_vouchers(BASE, chart, batch)
        self.assertGreater(to_cents(HUGE), 2 ** 53)
        huge_cents = to_cents(HUGE)
        shard_pairs = []
        for voucher in batch:
            shard = post_vouchers(BASE, copy.deepcopy(chart),
                                  copy.deepcopy([voucher]))
            for row in shard["accounts"]:
                shard_pairs.append(
                    (row["code"], to_cents(row["debit_turnover"]),
                     to_cents(row["credit_turnover"])))
        merged = {}
        for code, debit, credit in shard_pairs:
            bag = merged.setdefault(code, [0, 0])
            bag[0] += debit
            bag[1] += credit
        self.assertEqual(merged["6601"], [huge_cents, 0])
        self.assertEqual(merged["2202"], [to_cents("123.45"), huge_cents])
        full = posted_rows_by_code(posted_full)
        self.assertEqual(full["6601"]["debit_turnover"],
                         from_cents(merged["6601"][0]))
        self.assertEqual(full["2202"]["credit_turnover"],
                         from_cents(merged["2202"][1]))


# --------------------------------------------------------------------- #
# normalize_vouchers rows correspond exactly to the posting journal.
# --------------------------------------------------------------------- #
class NormalizeJournalCorrespondenceTests(unittest.TestCase):
    def assert_each_normalized_row_maps_to_a_journal_row(self, batch):
        chart = make_chart()
        normalized = normalize_vouchers(BASE, chart, copy.deepcopy(batch))
        posted = post_vouchers(BASE, chart, copy.deepcopy(batch))
        journal = posted["journal"]

        normalized_keys = [
            (voucher["voucher_id"], entry["line_no"])
            for voucher in normalized
            for entry in voucher["entries"]
        ]
        journal_keys = [
            (row["voucher_id"], row["line_no"]) for row in journal
        ]
        self.assertEqual(normalized_keys, journal_keys)
        self.assertEqual(len(normalized_keys), len(set(normalized_keys)))

        journal_index = {
            (row["voucher_id"], row["line_no"]): row for row in journal
        }
        for voucher in normalized:
            for entry in voucher["entries"]:
                row = journal_index[
                    (voucher["voucher_id"], entry["line_no"])]
                with self.subTest(voucher=voucher["voucher_id"],
                                  line=entry["line_no"]):
                    # Every entry-level fact the two documents share is
                    # rendered identically.
                    self.assertEqual(row["date"], voucher["date"])
                    self.assertEqual(row["account_code"],
                                     entry["account_code"])
                    self.assertEqual(row["account_name"],
                                     entry["account_name"])
                    self.assertEqual(row["summary"], entry["summary"])
                    self.assertEqual(row["debit"], entry["debit"])
                    self.assertEqual(row["credit"], entry["credit"])
            # Journal rows for one voucher carry the voucher date.
            rows = [row for row in journal
                    if row["voucher_id"] == voucher["voucher_id"]]
            self.assertTrue(rows)
            self.assertTrue(all(row["date"] == voucher["date"]
                                for row in rows))

        # No journal row exists without a normalized counterpart.
        self.assertEqual(
            sorted(journal_index),
            sorted((voucher["voucher_id"], entry["line_no"])
                   for voucher in normalized
                   for entry in voucher["entries"]))

    def test_rich_fixture_row_correspondence(self):
        self.assert_each_normalized_row_maps_to_a_journal_row(make_batch())

    def test_zeroing_fixture_row_correspondence(self):
        self.assert_each_normalized_row_maps_to_a_journal_row(
            make_zeroing_batch())

    def test_empty_batch_has_no_rows_on_either_side(self):
        chart = make_chart()
        self.assertEqual(normalize_vouchers(BASE, chart, []), [])
        self.assertEqual(post_vouchers(BASE, chart, [])["journal"], [])

    def test_line_numbers_restart_per_voucher_and_match_journal(self):
        chart, batch = make_chart(), make_batch()
        normalized = normalize_vouchers(BASE, chart, batch)
        posted = post_vouchers(BASE, chart, copy.deepcopy(batch))
        for voucher in normalized:
            self.assertEqual(
                [entry["line_no"] for entry in voucher["entries"]],
                list(range(1, len(voucher["entries"]) + 1)))
        seen_lines: dict[str, list[int]] = {}
        for row in posted["journal"]:
            seen_lines.setdefault(row["voucher_id"], []).append(
                row["line_no"])
        for voucher in normalized:
            self.assertEqual(
                seen_lines[voucher["voucher_id"]],
                [entry["line_no"] for entry in voucher["entries"]])


# --------------------------------------------------------------------- #
# Reordering: only voucher-ordered surfaces may follow the new order.
# --------------------------------------------------------------------- #
class ReorderInvarianceTests(unittest.TestCase):
    REORDERS = (
        ("reversed", lambda n: list(reversed(range(n)))),
        ("rotated", lambda n: list(range(1, n)) + [0]),
        ("interleaved",
         lambda n: list(range(0, n, 2)) + list(range(1, n, 2))),
    )

    def assert_reorder_invariants(self, chart, batch):
        chart_codes = [account["code"] for account in chart]
        normalized_base = normalize_vouchers(BASE, chart,
                                             copy.deepcopy(batch))
        posted_base = post_vouchers(BASE, chart, copy.deepcopy(batch))
        trial_base = build_trial_balance(BASE, chart, copy.deepcopy(batch))

        for label, permutation in self.REORDERS:
            order = permutation(len(batch))
            self.assertEqual(sorted(order), list(range(len(batch))))
            reordered = [batch[i] for i in order]

            normalized = normalize_vouchers(
                BASE, copy.deepcopy(chart), copy.deepcopy(reordered))
            posted = post_vouchers(
                BASE, copy.deepcopy(chart), copy.deepcopy(reordered))
            trial = build_trial_balance(
                BASE, copy.deepcopy(chart), copy.deepcopy(reordered))

            with self.subTest(reorder=label):
                # Voucher-level documents follow the new order exactly.
                self.assertEqual([v["voucher_id"] for v in normalized],
                                 [_voucher_id(batch[i]) for i in order])
                self.assertEqual(
                    [row["voucher_id"] for row in posted["journal"]],
                    list(itertools.chain.from_iterable(
                        [_voucher_id(batch[i])]
                        * _entry_count(batch[i]) for i in order)))

                # Each reordered voucher is the same document the full
                # batch produced for that voucher id.
                base_vouchers = {
                    voucher["voucher_id"]: voucher
                    for voucher in normalized_base
                }
                for voucher in normalized:
                    self.assertEqual(voucher,
                                     base_vouchers[voucher["voucher_id"]])

                # The journal as a multiset of rows is unchanged.
                self.assertEqual(
                    sorted(_journal_signature(row)
                           for row in posted["journal"]),
                    sorted(_journal_signature(row)
                           for row in posted_base["journal"]))

                # Account rows: same chart order, identical facts.
                self.assertEqual(
                    [row["code"] for row in posted["accounts"]],
                    chart_codes)
                self.assertEqual(
                    [row["code"] for row in trial["accounts"]],
                    chart_codes)
                self.assertEqual(posted["accounts"],
                                 posted_base["accounts"])
                self.assertEqual(trial["accounts"],
                                 trial_base["accounts"])
                self.assertEqual(trial["totals"], trial_base["totals"])

                # Turnover, ending direction and balance never move.
                for code in chart_codes:
                    base_row = posted_rows_by_code(posted_base)[code]
                    row = posted_rows_by_code(posted)[code]
                    for field in ("debit_turnover", "credit_turnover",
                                  "ending_side", "ending_balance"):
                        self.assertEqual(row[field], base_row[field],
                                         f"{code}.{field} under {label}")

    def test_rich_fixture_reorders(self):
        self.assert_reorder_invariants(make_chart(), make_batch())

    def test_zeroing_fixture_reorders(self):
        self.assert_reorder_invariants(make_chart(), make_zeroing_batch())

    def test_all_permutations_of_the_zeroing_fixture(self):
        # Three vouchers -> all six permutations checked exhaustively.
        chart, batch = make_chart(), make_zeroing_batch()
        posted_base = post_vouchers(BASE, chart, copy.deepcopy(batch))
        trial_base = build_trial_balance(BASE, chart, copy.deepcopy(batch))
        normalized_base = normalize_vouchers(
            BASE, chart, copy.deepcopy(batch))
        for permutation in itertools.permutations(range(len(batch))):
            reordered = [batch[i] for i in permutation]
            posted = post_vouchers(BASE, copy.deepcopy(chart),
                                   copy.deepcopy(reordered))
            trial = build_trial_balance(BASE, copy.deepcopy(chart),
                                        copy.deepcopy(reordered))
            normalized = normalize_vouchers(
                BASE, copy.deepcopy(chart), copy.deepcopy(reordered))
            self.assertEqual([v["voucher_id"] for v in normalized],
                             [_voucher_id(voucher) for voucher in reordered])
            self.assertEqual(posted["accounts"], posted_base["accounts"])
            self.assertEqual(trial["accounts"], trial_base["accounts"])
            self.assertEqual(trial["totals"], trial_base["totals"])
            self.assertEqual(
                sorted(_journal_signature(row)
                       for row in posted["journal"]),
                sorted(_journal_signature(row)
                       for row in posted_base["journal"]))
        # Base objects are still the original order.
        self.assertEqual([_voucher_id(v) for v in batch],
                         [v["voucher_id"] for v in normalized_base])

    def test_reverse_direction_balances_survive_reordering(self):
        # Opposite-to-normal ending directions must not be an artifact of
        # voucher order.
        chart, batch = make_chart(), make_batch()
        posted = post_vouchers(BASE, chart, list(reversed(batch)))
        rows = posted_rows_by_code(posted)
        # 1001 is debit-normal but only credited -> ends on the credit
        # side; 6001 is credit-normal but nets debit -> ends on debit.
        self.assertEqual(rows["1001"]["ending_side"], "credit")
        self.assertEqual(rows["1001"]["ending_balance"], "133.45")
        self.assertEqual(rows["6001"]["ending_side"], "debit")
        self.assertEqual(rows["6001"]["ending_balance"], "10.00")
        # 2202 stays on its normal credit side with the huge net.
        self.assertEqual(rows["2202"]["ending_side"], "credit")
        self.assertEqual(rows["2202"]["ending_balance"],
                         "9007199254740869.55")
        # The zeroing 备用金 account keeps its normal side at zero.
        self.assertEqual(rows["1221"]["ending_side"], "debit")
        self.assertEqual(rows["1221"]["ending_balance"], "0.00")
        # Unused active and unreferenced inactive accounts stay zero.
        self.assertEqual(rows["1901"]["ending_balance"], "0.00")
        self.assertEqual(rows["4001"]["ending_balance"], "0.00")
        self.assertEqual(rows["4001"]["ending_side"], "credit")


def _voucher_id(voucher):
    return voucher["voucher_id"]


def _entry_count(voucher):
    return len(voucher["entries"])


def _journal_signature(row):
    """Order-independent identity of one journal row for multiset compare."""
    return tuple(row[field] for field in (
        "voucher_id", "date", "line_no", "account_code", "account_name",
        "summary", "debit", "credit"))


# --------------------------------------------------------------------- #
# Required scenario rows: zeroing, opposite side, unused/inactive.
# --------------------------------------------------------------------- #
class ScenarioFactTests(unittest.TestCase):
    def test_cross_voucher_debit_credit_zeroing_account(self):
        chart, batch = make_chart(), make_zeroing_batch()
        posted, trial = post_vouchers(BASE, chart, batch), \
            build_trial_balance(BASE, chart, copy.deepcopy(batch))
        cash_posted = posted_rows_by_code(posted)["1001"]
        cash_trial = trial_rows_by_code(trial)["1001"]
        self.assertEqual(cash_posted["debit_turnover"], "60.00")
        self.assertEqual(cash_posted["credit_turnover"], "60.00")
        self.assertEqual(cash_posted["ending_side"], "debit")
        self.assertEqual(cash_posted["ending_balance"], "0.00")
        self.assertEqual(cash_trial["ending_debit"], "0.00")
        self.assertEqual(cash_trial["ending_credit"], "0.00")
        self.assertTrue(trial["totals"]["turnover_balanced"])
        self.assertTrue(trial["totals"]["ending_balanced"])

    def test_opposite_side_balances_in_both_shapes(self):
        chart, batch = make_chart(), make_batch()
        posted = post_vouchers(BASE, chart, batch)
        trial = build_trial_balance(BASE, chart, copy.deepcopy(batch))
        posted_rows, trial_rows = (posted_rows_by_code(posted),
                                   trial_rows_by_code(trial))
        # Credit-normal liability sitting on credit, but partially repaid.
        self.assertEqual(posted_rows["2202"]["ending_side"], "credit")
        self.assertEqual(posted_rows["2202"]["ending_balance"],
                         "9007199254740869.55")
        self.assertEqual(trial_rows["2202"]["ending_debit"], "0.00")
        self.assertEqual(trial_rows["2202"]["ending_credit"],
                         "9007199254740869.55")
        # Debit-normal cash pushed fully to credit.
        self.assertEqual(posted_rows["1001"]["ending_side"], "credit")
        self.assertEqual(trial_rows["1001"]["ending_debit"], "0.00")
        self.assertEqual(trial_rows["1001"]["ending_credit"], "133.45")

    def test_unused_active_and_unreferenced_inactive_accounts(self):
        chart = make_chart()

        posted = post_vouchers(BASE, copy.deepcopy(chart), make_batch())
        posted_rows = posted_rows_by_code(posted)
        self.assertEqual([row["code"] for row in posted["accounts"]],
                         [account["code"] for account in chart])
        for code, side in (("1901", "debit"), ("4001", "credit")):
            row = posted_rows[code]
            self.assertEqual(row["debit_turnover"], "0.00")
            self.assertEqual(row["credit_turnover"], "0.00")
            self.assertEqual(row["ending_side"], side)
            self.assertEqual(row["ending_balance"], "0.00")

        # The trial balance has no side field: both ending columns are 0.
        trial = build_trial_balance(BASE, copy.deepcopy(chart),
                                    make_batch())
        trial_rows = trial_rows_by_code(trial)
        self.assertEqual([row["code"] for row in trial["accounts"]],
                         [account["code"] for account in chart])
        for code in ("1901", "4001"):
            row = trial_rows[code]
            self.assertEqual(row["debit_turnover"], "0.00")
            self.assertEqual(row["credit_turnover"], "0.00")
            self.assertEqual(row["ending_debit"], "0.00")
            self.assertEqual(row["ending_credit"], "0.00")

        # The same zeros survive sharding: neither account is ever touched.
        for voucher in make_batch():
            shard_trial = build_trial_balance(
                BASE, copy.deepcopy(chart), copy.deepcopy([voucher]))
            rows = trial_rows_by_code(shard_trial)
            self.assertEqual(rows["1901"]["debit_turnover"], "0.00")
            self.assertEqual(rows["4001"]["credit_turnover"], "0.00")


# --------------------------------------------------------------------- #
# Fresh containers, character-identical serialization, no sharing.
# --------------------------------------------------------------------- #
class FreshContainerAndSerializationTests(unittest.TestCase):
    def test_all_three_entries_character_identical_on_fresh_inputs(self):
        for _ in range(3):
            results_a = call_all(make_chart(), make_batch())
            results_b = call_all(make_chart(), make_batch())
            for result_a, result_b in zip(results_a, results_b):
                self.assertEqual(result_a, result_b)
                self.assertEqual(
                    json.dumps(result_a, **JSON_KW),
                    json.dumps(result_b, **JSON_KW))
                self.assertEqual(
                    json.dumps(result_a, **JSON_KW).encode("utf-8"),
                    json.dumps(result_b, **JSON_KW).encode("utf-8"))

    def test_no_two_calls_share_a_mutable_container(self):
        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                first = entry_point(BASE, make_chart(), make_batch())
                second = entry_point(BASE, make_chart(), make_batch())
                self.assertEqual(first, second)
                self.assertTrue(
                    mutable_ids(first).isdisjoint(mutable_ids(second)),
                    f"{entry_point.__name__}: calls share a container")
                self.assertIsNot(first, second)

    def test_entries_share_no_containers_with_each_other_or_inputs(self):
        chart, batch = make_chart(), make_batch()
        normalized, posted, trial = call_all(chart, batch)
        all_result_ids = (mutable_ids(normalized)
                          | mutable_ids(posted) | mutable_ids(trial))
        # Between the three entries.
        self.assertTrue(mutable_ids(normalized).isdisjoint(
            mutable_ids(posted)))
        self.assertTrue(mutable_ids(normalized).isdisjoint(
            mutable_ids(trial)))
        self.assertTrue(mutable_ids(posted).isdisjoint(
            mutable_ids(trial)))
        # Versus the caller's raw objects.
        self.assertTrue(all_result_ids.isdisjoint(
            mutable_ids(chart) | mutable_ids(batch)))

    def test_deep_mutation_never_reaches_a_later_call_of_any_entry(self):
        chart, batch = make_chart(), make_batch()
        snapshots = [
            copy.deepcopy(entry_point(BASE, chart, batch))
            for entry_point in ENTRY_POINTS
        ]
        results = [
            entry_point(BASE, chart, batch) for entry_point in ENTRY_POINTS
        ]
        for result in results:
            corrupt_deep(result)
        chart_copy = copy.deepcopy(chart)
        batch_copy = copy.deepcopy(batch)
        for entry_point, snapshot, corrupted in zip(
                ENTRY_POINTS, snapshots, results):
            with self.subTest(entry_point=entry_point.__name__):
                recomputed = entry_point(BASE, chart, batch)
                self.assertEqual(recomputed, snapshot)
                self.assertNotEqual(corrupted, snapshot)
        # Raw inputs remain exactly the objects originally supplied.
        self.assertEqual(chart, chart_copy)
        self.assertEqual(batch, batch_copy)
        self.assertEqual(
            [account["code"] for account in chart],
            [account["code"] for account in chart_copy])
        self.assertEqual(
            [voucher["voucher_id"] for voucher in batch],
            [voucher["voucher_id"] for voucher in batch_copy])

    def test_json_text_is_stable_including_huge_and_trial_totals(self):
        _, posted, trial = call_all(make_chart(), make_batch())
        text_posted = json.dumps(posted, **JSON_KW)
        text_trial = json.dumps(trial, **JSON_KW)
        for _ in range(2):
            _, posted_again, trial_again = call_all(
                make_chart(), make_batch())
            self.assertEqual(text_posted,
                             json.dumps(posted_again, **JSON_KW))
            self.assertEqual(text_trial,
                             json.dumps(trial_again, **JSON_KW))
        self.assertIn(HUGE, text_posted)
        self.assertIn(HUGE, text_trial)
        self.assertIn("9007199254740869.55", text_trial)
        # Totals are balanced for the balanced rich fixture.
        self.assertIs(trial["totals"]["turnover_balanced"], True)
        self.assertIs(trial["totals"]["ending_balanced"], True)

    def test_empty_batch_is_character_identical_across_entries_calls(self):
        chart = make_chart()
        normalized_runs = [normalize_vouchers(BASE, make_chart(), [])
                           for _ in range(2)]
        posted_runs = [post_vouchers(BASE, make_chart(), [])
                       for _ in range(2)]
        trial_runs = [build_trial_balance(BASE, make_chart(), [])
                      for _ in range(2)]
        for runs in (normalized_runs, posted_runs, trial_runs):
            self.assertEqual(runs[0], runs[1])
            self.assertEqual(json.dumps(runs[0], **JSON_KW),
                             json.dumps(runs[1], **JSON_KW))
            self.assertTrue(mutable_ids(runs[0]).isdisjoint(
                mutable_ids(runs[1])))


# --------------------------------------------------------------------- #
# Bad shards: all three entries share the existing first leaf boundary.
# --------------------------------------------------------------------- #
class BadShardBoundaryTests(unittest.TestCase):
    def assert_all_three_raise_same_leaf(self, expected_exc, chart,
                                         good_batch, bad_shard):
        """Each entry fails identically on the bad shard alone.

        The valid batch and the bad shard merged together must also fail
        the same way; nothing is returned and the bad shard can never be
        merged into a summary.  Raw containers stay untouched.
        """
        messages = []
        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                chart_snapshot = copy.deepcopy(chart)
                bad_snapshot = copy.deepcopy(bad_shard)
                with self.assertRaises(LedgerEngineError) as caught:
                    entry_point(BASE, chart, bad_shard)
                self.assertIs(type(caught.exception), expected_exc,
                              f"expected leaf {expected_exc.__name__}, "
                              f"got {type(caught.exception).__name__}")
                messages.append(str(caught.exception))
                self.assertEqual(chart, chart_snapshot)
                self.assertEqual(bad_shard, bad_snapshot)

        self.assertEqual(len(set(messages)), 1, messages)

        # Appending the bad shard as a later partition must not produce a
        # partial merge either: all three entries still report the same
        # leaf boundary with one shared message (its voucher index is the
        # position in the merged batch, so it is not the standalone text).
        merged_messages = []
        for entry_point in ENTRY_POINTS:
            with self.subTest(merged=entry_point.__name__):
                merged_batch = copy.deepcopy(good_batch) + \
                    copy.deepcopy(bad_shard)
                with self.assertRaises(LedgerEngineError) as caught:
                    entry_point(BASE, copy.deepcopy(chart), merged_batch)
                self.assertIs(type(caught.exception), expected_exc)
                merged_messages.append(str(caught.exception))
        self.assertEqual(len(set(merged_messages)), 1, merged_messages)
        self.assertIsNotNone(messages[0])

    def test_malformed_voucher_shape(self):
        bad = make_voucher("V-BAD", "2024-05-20", [
            make_entry("6601", "只有一条分录", debit="1.00"),
        ])
        self.assert_all_three_raise_same_leaf(
            VoucherFormatError, make_chart(), make_batch(), [bad])

    def test_duplicate_voucher_id_inside_shard(self):
        bad = [
            make_voucher("V-DUP", "2024-05-20", [
                make_entry("6601", "借", debit="1.00"),
                make_entry("2202", "贷", credit="1.00"),
            ]),
            make_voucher("V-DUP", "2024-05-21", [
                make_entry("6601", "再次借", debit="2.00"),
                make_entry("2202", "再次贷", credit="2.00"),
            ]),
        ]
        self.assert_all_three_raise_same_leaf(
            DuplicateVoucherError, make_chart(), make_batch(), bad)

    def test_currency_mismatch(self):
        bad = [make_voucher("V-FX", "2024-05-20", [
            make_entry("6601", "外币借", debit="1.00"),
            make_entry("2202", "外币贷", credit="1.00"),
        ], currency="USD")]
        self.assert_all_three_raise_same_leaf(
            UnsupportedCurrencyError, make_chart(), make_batch(), bad)

    def test_unknown_account(self):
        bad = [make_voucher("V-U", "2024-05-20", [
            make_entry("9999", "未知科目", debit="1.00"),
            make_entry("2202", "平衡", credit="1.00"),
        ])]
        self.assert_all_three_raise_same_leaf(
            UnknownAccountError, make_chart(), make_batch(), bad)

    def test_inactive_account(self):
        bad = [make_voucher("V-I", "2024-05-20", [
            make_entry("4001", "停用科目", debit="1.00"),
            make_entry("2202", "平衡", credit="1.00"),
        ])]
        self.assert_all_three_raise_same_leaf(
            InactiveAccountError, make_chart(), make_batch(), bad)

    def test_illegal_amount(self):
        bad = [make_voucher("V-A", "2024-05-20", [
            make_entry("6601", "三位小数", debit="1.005"),
            make_entry("2202", "配平", credit="1.00"),
        ])]
        self.assert_all_three_raise_same_leaf(
            InvalidEntryAmountError, make_chart(), make_batch(), bad)

    def test_two_sided_entry_is_an_amount_error(self):
        bad_voucher = make_voucher("V-TWO", "2024-05-20", [
            make_entry("6601", "借贷都有", debit="1.00", credit="1.00"),
            make_entry("2202", "配平", credit="2.00"),
        ])
        self.assert_all_three_raise_same_leaf(
            InvalidEntryAmountError, make_chart(), make_batch(),
            [bad_voucher])

    def test_unbalanced_voucher(self):
        bad = [make_voucher("V-B", "2024-05-20", [
            make_entry("6601", "借五元", debit="5.00"),
            make_entry("2202", "贷四元", credit="4.00"),
        ])]
        self.assert_all_three_raise_same_leaf(
            UnbalancedVoucherError, make_chart(), make_batch(), bad)

    def test_good_shards_still_post_when_a_separate_bad_shard_fails(self):
        # The accounting-meaning-preserving fact: good shards alone are
        # valid; the bad shard is rejected on its own and must never be
        # silently merged.  Processing good shards still reconciles.
        chart = make_chart()
        batch = make_batch()
        midpoint = len(batch) // 2
        good_shards = [batch[:midpoint], batch[midpoint:]]
        bad_shard = [make_voucher("V-BAD", "2024-05-20", [
            make_entry("6601", "借五元", debit="5.00"),
            make_entry("2202", "贷四元", credit="4.00"),
        ])]

        merged = {account["code"]: [0, 0] for account in chart}
        for shard in good_shards:
            posted = post_vouchers(BASE, copy.deepcopy(chart),
                                   copy.deepcopy(shard))
            for row in posted["accounts"]:
                merged[row["code"]][0] += to_cents(row["debit_turnover"])
                merged[row["code"]][1] += to_cents(row["credit_turnover"])
        full = post_vouchers(BASE, chart, copy.deepcopy(batch))
        for row in full["accounts"]:
            self.assertEqual(from_cents(merged[row["code"]][0]),
                             row["debit_turnover"])
            self.assertEqual(from_cents(merged[row["code"]][1]),
                             row["credit_turnover"])

        for entry_point in ENTRY_POINTS:
            with self.subTest(entry_point=entry_point.__name__):
                with self.assertRaises(UnbalancedVoucherError):
                    entry_point(BASE, copy.deepcopy(chart),
                                copy.deepcopy(bad_shard))
                # And mixing good + bad across a shard boundary fails too.
                mixed = good_shards[0] + bad_shard + good_shards[1]
                with self.assertRaises(UnbalancedVoucherError):
                    entry_point(BASE, copy.deepcopy(chart),
                                copy.deepcopy(mixed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
