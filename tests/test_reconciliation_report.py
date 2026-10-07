"""Black-box contract tests for ``build_reconciliation_report``.

These tests pin the book-vs-statement reconciliation feature purely
through the public package surface:

* the four fixed outcome envelopes and their precedence;
* all seven difference categories and the three match methods;
* identity-first matching, the identity-less attribute fallback and
  ambiguity for every non-unique decision (never an input-order pick);
* cutoff scoping: effective-by-cutoff only, in-scope reversal/offset
  cancellation, historical amounts kept at their recorded rate;
* per-currency totals (different currencies are never summed together),
  the single base-currency difference and the recorded rate trail;
* empty snapshots, duplicate source ids, missing book/account and the
  unclosed-historical-period conflict;
* byte-level reproducibility across input reorderings, dict insertion
  orders and PYTHONHASHSEED, fresh disjoint containers and no mutation.

Standard library only; CPython 3.10+.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest

from ledger_engine import (
    DIFF_AMBIGUOUS,
    DIFF_AMOUNT_MISMATCH,
    DIFF_BASE_AMOUNT_MISMATCH,
    DIFF_CURRENCY_MISMATCH,
    DIFF_EXTERNAL_MISSING,
    DIFF_LEDGER_MISSING,
    DIFF_MATCHED,
    MATCH_ATTRIBUTES,
    MATCH_SOURCE_ID,
    MATCH_VOUCHER_REF,
    OUTCOME_INVALID_INPUT,
    OUTCOME_PERIOD_STATUS_CONFLICT,
    OUTCOME_RECONCILED,
    OUTCOME_TARGET_NOT_FOUND,
    build_reconciliation_report,
)

BASE = "CNY"
JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}

CATEGORIES = (
    DIFF_MATCHED, DIFF_LEDGER_MISSING, DIFF_EXTERNAL_MISSING,
    DIFF_AMOUNT_MISMATCH, DIFF_CURRENCY_MISMATCH,
    DIFF_BASE_AMOUNT_MISMATCH, DIFF_AMBIGUOUS,
)


# --------------------------------------------------------------------- #
# Fixture factories -- fresh containers, deliberately scrambled key order.
# --------------------------------------------------------------------- #
def make_chart():
    return [
        {"active": True, "code": "1001", "name": "库存现金",
         "normal_side": "debit"},
        {"normal_side": "credit", "name": "应付账款", "active": True,
         "code": "2202"},
        {"code": "6601", "normal_side": "debit", "name": "管理费用",
         "active": True},
        {"code": "4001", "active": False, "name": "停用科目",
         "normal_side": "credit"},
    ]


def fact(fact_id, account="1001", date="2024-05-10", currency="CNY",
         amount="100.00", **extra):
    row = {"amount": amount, "account_code": account, "date": date,
           "fact_id": fact_id, "currency": currency}
    row.update(extra)
    return row


def detail(source_id, date="2024-05-10", currency="CNY", amount="100.00",
           **extra):
    row = {"currency": currency, "amount": amount, "source_id": source_id,
           "date": date}
    row.update(extra)
    return row


def make_request(facts, details, *, cutoff="2024-05-31", account="1001",
                 book_set="B1", periods=None,
                 base_currency=BASE, chart=None):
    request = {
        "external_details": details,
        "cutoff": cutoff,
        "account_code": account,
        "base_currency": base_currency,
        "book": {"book_set_id": book_set, "facts": facts},
        "book_set_id": book_set,
        "chart_of_accounts": make_chart() if chart is None else chart,
    }
    if periods is not None:
        request["periods"] = periods
    return request


def report(facts, details, **kwargs):
    return build_reconciliation_report(make_request(facts, details, **kwargs))


def section(result, category):
    return result["differences"][category]


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
# Public surface
# --------------------------------------------------------------------- #
class PublicSurfaceTests(unittest.TestCase):
    def test_constants_are_stable_literals(self):
        self.assertEqual(OUTCOME_RECONCILED, "reconciled")
        self.assertEqual(OUTCOME_TARGET_NOT_FOUND, "target_not_found")
        self.assertEqual(OUTCOME_PERIOD_STATUS_CONFLICT,
                         "period_status_conflict")
        self.assertEqual(OUTCOME_INVALID_INPUT, "invalid_input")
        self.assertEqual(
            CATEGORIES,
            ("matched", "ledger_missing", "external_missing",
             "amount_mismatch", "currency_mismatch",
             "base_amount_mismatch", "ambiguous"))
        self.assertEqual((MATCH_SOURCE_ID, MATCH_VOUCHER_REF,
                          MATCH_ATTRIBUTES),
                         ("source_id", "voucher_ref", "attributes"))

    def test_seven_sections_always_present_in_fixed_order(self):
        result = report([], [])
        self.assertEqual(result["outcome"], OUTCOME_RECONCILED)
        self.assertEqual(list(result["differences"]), list(CATEGORIES))
        for category in CATEGORIES:
            self.assertEqual(section(result, category), [])

    def test_top_level_key_insertion_order(self):
        result = report([], [])
        self.assertEqual(
            list(result),
            ["outcome", "book_set_id", "account_code", "base_currency",
             "cutoff", "cutoff_period_id", "differences", "summary"])
        self.assertEqual(
            list(result["summary"]),
            ["by_currency", "base_currency", "base_currency_difference",
             "counts"])
        self.assertEqual(list(result["summary"]["counts"]),
                         list(CATEGORIES))


# --------------------------------------------------------------------- #
# Matched: identity methods and attribute fallback
# --------------------------------------------------------------------- #
class MatchedTests(unittest.TestCase):
    def test_match_via_source_id_carried_on_ledger_fact(self):
        facts = [fact("F1", voucher_id="V1", line_no=2, voucher_ref="S1")]
        result = report(facts, [detail("S1")])
        items = section(result, DIFF_MATCHED)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["match_method"], MATCH_SOURCE_ID)
        self.assertEqual(item["external"]["source_id"], "S1")
        self.assertEqual(item["ledger"]["fact_id"], "F1")
        self.assertEqual(item["ledger"]["voucher_id"], "V1")
        self.assertEqual(item["ledger"]["line_no"], 2)
        self.assertEqual(item["category"], DIFF_MATCHED)

    def test_match_via_external_voucher_reference(self):
        facts = [fact("F1", voucher_id="V7")]
        result = report(facts, [detail("S7", voucher_ref="V7")])
        items = section(result, DIFF_MATCHED)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["match_method"], MATCH_VOUCHER_REF)

    def test_attribute_fallback_without_business_identity(self):
        facts = [fact("F1", date="2024-05-11", amount="42.00")]
        result = report(facts, [detail("S1", date="2024-05-11",
                                       amount="42.00")])
        items = section(result, DIFF_MATCHED)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["match_method"], MATCH_ATTRIBUTES)

    def test_attribute_fallback_requires_date_currency_amount(self):
        # Same currency and amount but different date -> not a match.
        facts = [fact("F1", date="2024-05-11", amount="42.00")]
        same_day_wrong_amount = report(
            facts, [detail("S1", date="2024-05-11", amount="43.00")])
        self.assertEqual(section(same_day_wrong_amount, DIFF_MATCHED), [])
        self.assertEqual(len(section(same_day_wrong_amount,
                                    DIFF_LEDGER_MISSING)), 1)
        self.assertEqual(len(section(same_day_wrong_amount,
                                    DIFF_EXTERNAL_MISSING)), 1)
        wrong_day = report(
            [fact("F1", date="2024-05-11", amount="42.00")],
            [detail("S1", date="2024-05-12", amount="42.00")])
        self.assertEqual(section(wrong_day, DIFF_MATCHED), [])
        self.assertEqual(len(section(wrong_day, DIFF_LEDGER_MISSING)), 1)

    def test_asserted_but_unresolved_identity_never_uses_attributes(self):
        # The external detail asserts voucher V9; no fact names V9.  An
        # attribute-equal fact exists, but identity is missing: this must
        # be ledger_missing and the fact external_missing, never matched.
        facts = [fact("F1", voucher_id="V8", date="2024-05-10",
                      amount="42.00")]
        details_ = [detail("S1", voucher_ref="V9", date="2024-05-10",
                           amount="42.00")]
        result = report(facts, details_)
        self.assertEqual(section(result, DIFF_MATCHED), [])
        self.assertEqual(
            [i["external"]["source_id"]
             for i in section(result, DIFF_LEDGER_MISSING)], ["S1"])
        self.assertEqual(
            [i["ledger"]["fact_id"]
             for i in section(result, DIFF_EXTERNAL_MISSING)], ["F1"])

    def test_identity_takes_precedence_when_attributes_differ(self):
        # Identity resolves to F1; its amount differs, so it is an
        # amount mismatch rather than an attribute match on a look-alike.
        facts = [
            fact("F1", voucher_ref="S1", date="2024-05-10",
                 amount="100.00"),
            fact("F2", date="2024-05-10", amount="80.00"),
        ]
        result = report(facts, [detail("S1", amount="80.00")])
        mismatches = section(result, DIFF_AMOUNT_MISMATCH)
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["match_method"], MATCH_SOURCE_ID)
        self.assertEqual(mismatches[0]["ledger"]["fact_id"], "F1")
        # F2 has no external counterpart; nothing was stolen by identity.
        self.assertEqual(
            [i["ledger"]["fact_id"]
             for i in section(result, DIFF_EXTERNAL_MISSING)], ["F2"])


# --------------------------------------------------------------------- #
# Missing records
# --------------------------------------------------------------------- #
class MissingTests(unittest.TestCase):
    def test_ledger_missing_external_without_fact(self):
        result = report([], [detail("S1")])
        items = section(result, DIFF_LEDGER_MISSING)
        self.assertEqual(len(items), 1)
        self.assertEqual(list(items[0]), ["category", "external"])
        self.assertEqual(items[0]["category"], DIFF_LEDGER_MISSING)

    def test_external_missing_fact_without_detail(self):
        result = report([fact("F1")], [])
        items = section(result, DIFF_EXTERNAL_MISSING)
        self.assertEqual(len(items), 1)
        self.assertEqual(list(items[0]), ["category", "ledger"])
        self.assertEqual(items[0]["ledger"]["fact_id"], "F1")

    def test_empty_snapshot_lists_all_in_scope_facts(self):
        facts = [
            fact("F1", date="2024-05-01", amount="10.00"),
            fact("F2", date="2024-05-02", amount="20.00"),
            fact("F3", account="2202", date="2024-05-02",
                 amount="20.00"),
            fact("F4", date="2024-06-15", amount="30.00"),
        ]
        result = report(facts, [], cutoff="2024-05-31")
        ids = [i["ledger"]["fact_id"]
               for i in section(result, DIFF_EXTERNAL_MISSING)]
        # Same account only, effective by cutoff only; F3 is another
        # account and F4 is after the cutoff.
        self.assertEqual(ids, ["F1", "F2"])

    def test_empty_snapshot_and_empty_book_reconcile_empty(self):
        result = report([], [])
        self.assertEqual(result["outcome"], OUTCOME_RECONCILED)
        for category in CATEGORIES:
            self.assertEqual(section(result, category), [])


# --------------------------------------------------------------------- #
# Amount, currency and base-currency differences
# --------------------------------------------------------------------- #
class DifferenceCategoryTests(unittest.TestCase):
    def test_amount_mismatch_carries_both_amounts(self):
        facts = [fact("F1", voucher_ref="S1", amount="100.00")]
        result = report(facts, [detail("S1", amount="90.00")])
        items = section(result, DIFF_AMOUNT_MISMATCH)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["external_amount"],
                         {"currency": "CNY", "amount": "90.00"})
        self.assertEqual(item["ledger_amount"],
                         {"currency": "CNY", "amount": "100.00"})
        self.assertIsNone(item["base_currency_difference"])

    def test_currency_mismatch(self):
        facts = [fact("F1", voucher_id="V1", currency="EUR",
                      amount="10.00")]
        result = report(facts, [detail("S1", voucher_ref="V1",
                                       currency="USD", amount="10.00")])
        items = section(result, DIFF_CURRENCY_MISMATCH)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["external_amount"]["currency"], "USD")
        self.assertEqual(items[0]["ledger_amount"]["currency"], "EUR")

    def test_base_amount_mismatch_uses_recorded_rate(self):
        facts = [fact(
            "F1", voucher_id="V1", currency="USD", amount="10.00",
            base_amount="73.00",
            rate={"rate": "7.2", "rate_date": "2024-05-10",
                  "rate_source": "historical-table"})]
        result = report(facts, [detail("S1", voucher_ref="V1",
                                       currency="USD", amount="10.00")])
        items = section(result, DIFF_BASE_AMOUNT_MISMATCH)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["expected_base_amount"], "72.00")
        self.assertEqual(item["ledger_base_amount"], "73.00")
        self.assertEqual(item["base_currency_difference"], "1.00")
        self.assertEqual(item["ledger"]["rate"],
                         {"rate": "7.2", "rate_date": "2024-05-10",
                          "rate_source": "historical-table"})

    def test_half_up_rounding_matches_recorded_amount(self):
        # 0.34 foreign * 7.2 = 2.448 base -> ROUND_HALF_UP 2.45; the
        # saved 2.45 agrees, so it is matched rather than a base gap.
        facts = [fact(
            "F1", voucher_id="V1", currency="USD", amount="0.34",
            base_amount="2.45", rate={"rate": "7.2"})]
        result = report(facts, [detail("S1", voucher_ref="V1",
                                       currency="USD", amount="0.34")])
        self.assertEqual(len(section(result, DIFF_BASE_AMOUNT_MISMATCH)), 0)
        self.assertEqual(len(section(result, DIFF_MATCHED)), 1)

    def test_base_currency_amount_compared_for_base_currency_facts(self):
        facts = [fact("F1", voucher_ref="S1", currency="CNY",
                      amount="100.00", base_amount="100.00")]
        result = report(facts, [detail("S1", currency="CNY",
                                       amount="100.00")])
        self.assertEqual(len(section(result, DIFF_MATCHED)), 1)

    def test_voucher_reference_trail_is_present_on_every_pair(self):
        facts = [fact("F1", voucher_id="V-2024-007", line_no=3,
                      voucher_ref="S1")]
        result = report(facts, [detail("S1")])
        ledger = section(result, DIFF_MATCHED)[0]["ledger"]
        self.assertEqual(ledger["voucher_id"], "V-2024-007")
        self.assertEqual(ledger["line_no"], 3)
        self.assertEqual(ledger["voucher_ref"], "S1")


# --------------------------------------------------------------------- #
# Ambiguity: non-unique decisions never pick by input order
# --------------------------------------------------------------------- #
class AmbiguityTests(unittest.TestCase):
    def test_two_facts_one_external_on_identical_attributes(self):
        facts = [
            fact("F1", date="2024-05-10", amount="5.00"),
            fact("F2", date="2024-05-10", amount="5.00"),
        ]
        result = report(facts, [detail("S1", date="2024-05-10",
                                       amount="5.00")])
        items = section(result, DIFF_AMBIGUOUS)
        self.assertEqual(len(items), 1)
        self.assertEqual([d["source_id"] for d in items[0]["external"]],
                         ["S1"])
        self.assertEqual([d["fact_id"] for d in items[0]["ledger"]],
                         ["F1", "F2"])
        self.assertEqual(section(result, DIFF_MATCHED), [])

    def test_two_externals_one_fact_on_identical_attributes(self):
        facts = [fact("F1", date="2024-05-10", amount="5.00")]
        details_ = [
            detail("S1", date="2024-05-10", amount="5.00"),
            detail("S2", date="2024-05-10", amount="5.00"),
        ]
        result = report(facts, details_)
        items = section(result, DIFF_AMBIGUOUS)
        self.assertEqual(len(items), 1)
        self.assertEqual([d["source_id"] for d in items[0]["external"]],
                         ["S1", "S2"])
        self.assertEqual([d["fact_id"] for d in items[0]["ledger"]],
                         ["F1"])

    def test_two_by_two_attribute_bucket_is_one_ambiguous_component(self):
        facts = [
            fact("F1", date="2024-05-10", amount="5.00"),
            fact("F2", date="2024-05-10", amount="5.00"),
        ]
        details_ = [
            detail("S1", date="2024-05-10", amount="5.00"),
            detail("S2", date="2024-05-10", amount="5.00"),
        ]
        result = report(facts, details_)
        items = section(result, DIFF_AMBIGUOUS)
        self.assertEqual(len(items), 1)
        self.assertEqual([d["source_id"] for d in items[0]["external"]],
                         ["S1", "S2"])
        self.assertEqual([d["fact_id"] for d in items[0]["ledger"]],
                         ["F1", "F2"])

    def test_identity_fan_out_is_ambiguous(self):
        # Two ledger facts claim the same external source id.
        facts = [
            fact("F1", voucher_ref="S1", amount="5.00"),
            fact("F2", voucher_ref="S1", amount="5.00"),
        ]
        result = report(facts, [detail("S1", amount="5.00")])
        items = section(result, DIFF_AMBIGUOUS)
        self.assertEqual(len(items), 1)
        self.assertEqual([d["fact_id"] for d in items[0]["ledger"]],
                         ["F1", "F2"])
        self.assertEqual([d["source_id"] for d in items[0]["external"]],
                         ["S1"])

    def test_ambiguous_records_are_not_also_listed_as_missing(self):
        facts = [
            fact("F1", date="2024-05-10", amount="5.00"),
            fact("F2", date="2024-05-10", amount="5.00"),
        ]
        result = report(facts, [detail("S1", date="2024-05-10",
                                       amount="5.00")])
        self.assertEqual(section(result, DIFF_EXTERNAL_MISSING), [])
        self.assertEqual(section(result, DIFF_LEDGER_MISSING), [])

    def test_unambiguous_attribute_pairs_coexist_with_ambiguous_bucket(self):
        facts = [
            fact("F1", date="2024-05-10", amount="5.00"),
            fact("F2", date="2024-05-10", amount="5.00"),
            fact("F3", date="2024-05-11", amount="7.00"),
        ]
        details_ = [
            detail("S1", date="2024-05-10", amount="5.00"),
            detail("S2", date="2024-05-10", amount="5.00"),
            detail("S3", date="2024-05-11", amount="7.00"),
        ]
        result = report(facts, details_)
        self.assertEqual(len(section(result, DIFF_AMBIGUOUS)), 1)
        matched = section(result, DIFF_MATCHED)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["ledger"]["fact_id"], "F3")
        self.assertEqual(matched[0]["match_method"], MATCH_ATTRIBUTES)


# --------------------------------------------------------------------- #
# Cutoff scoping and reversals
# --------------------------------------------------------------------- #
class CutoffAndReversalTests(unittest.TestCase):
    def _pair(self, amount, **extra):
        return fact("F1", amount=amount, **extra), fact(
            "F2", date="2024-05-20", effective_date="2024-05-20",
            amount=f"-{amount}")

    def test_fact_effective_after_cutoff_is_excluded(self):
        facts = [
            fact("F1", date="2024-05-10", effective_date="2024-06-05",
                 amount="10.00"),
            fact("F2", date="2024-05-02", amount="20.00"),
        ]
        result = report(facts, [], cutoff="2024-05-31")
        self.assertEqual(
            [i["ledger"]["fact_id"]
             for i in section(result, DIFF_EXTERNAL_MISSING)], ["F2"])

    def test_reversal_after_cutoff_does_not_erase_history(self):
        f1, f2 = self._pair("100.00")
        f1 = {**f1, "reversed_by": ["F2"]}
        result = report([f1, f2], [], cutoff="2024-05-15")
        ids = [i["ledger"]["fact_id"]
               for i in section(result, DIFF_EXTERNAL_MISSING)]
        self.assertEqual(ids, ["F1"])

    def test_reversal_within_scope_cancels_both_sides(self):
        f1, f2 = self._pair("100.00")
        f1 = {**f1, "reversed_by": ["F2"]}
        result = report([f1, f2], [], cutoff="2024-05-31")
        self.assertEqual(section(result, DIFF_EXTERNAL_MISSING), [])
        self.assertEqual(result["summary"]["counts"][DIFF_EXTERNAL_MISSING],
                         0)

    def test_symmetric_offsets_within_scope_cancel(self):
        f1 = fact("F1", date="2024-05-10", amount="100.00",
                  offsets=[{"fact_id": "F2", "amount": "-100.00"}])
        f2 = fact("F2", date="2024-05-12", amount="-100.00",
                  offsets=[{"fact_id": "F1", "amount": "100.00"}])
        result = report([f1, f2], [], cutoff="2024-05-31")
        self.assertEqual(section(result, DIFF_EXTERNAL_MISSING), [])

    def test_offset_after_cutoff_does_not_cancel(self):
        f1 = fact("F1", date="2024-05-10", amount="100.00",
                  offsets=[{"fact_id": "F2", "amount": "-100.00"}])
        f2 = fact("F2", date="2024-05-20", effective_date="2024-05-20",
                  amount="-100.00",
                  offsets=[{"fact_id": "F1", "amount": "100.00"}])
        result = report([f1, f2], [], cutoff="2024-05-15")
        ids = [i["ledger"]["fact_id"]
               for i in section(result, DIFF_EXTERNAL_MISSING)]
        self.assertEqual(ids, ["F1"])

    def test_other_account_facts_are_never_in_scope(self):
        facts = [
            fact("F1", account="1001", amount="10.00"),
            fact("F2", account="2202", amount="10.00"),
        ]
        result = report(facts, [], account="1001")
        self.assertEqual(
            [i["ledger"]["fact_id"]
             for i in section(result, DIFF_EXTERNAL_MISSING)], ["F1"])


# --------------------------------------------------------------------- #
# Summary: per-currency totals, base difference and counts
# --------------------------------------------------------------------- #
class SummaryTests(unittest.TestCase):
    def test_totals_are_separate_per_currency_and_never_mixed(self):
        facts = [
            fact("F1", voucher_ref="S1", currency="CNY", amount="100.00"),
            fact("F2", currency="USD", amount="50.00"),
        ]
        details_ = [
            detail("S1", currency="CNY", amount="90.00"),
            detail("S2", currency="USD", amount="50.00"),
        ]
        result = report(facts, details_)
        by_currency = {row["currency"]: row
                       for row in result["summary"]["by_currency"]}
        self.assertEqual(sorted(by_currency), ["CNY", "USD"])
        self.assertEqual(by_currency["CNY"]["ledger_total"], "100.00")
        self.assertEqual(by_currency["CNY"]["external_total"], "90.00")
        self.assertEqual(by_currency["CNY"]["difference"], "10.00")
        self.assertEqual(by_currency["USD"]["difference"], "0.00")
        self.assertEqual(result["summary"]["base_currency"], BASE)

    def test_signed_amounts_sum_exactly(self):
        facts = [
            fact("F1", date="2024-05-10", currency="USD", amount="50.00"),
            fact("F2", date="2024-05-11", currency="USD", amount="-20.00"),
        ]
        details_ = [
            detail("S1", date="2024-05-10", currency="USD",
                   amount="50.00"),
            detail("S2", date="2024-05-11", currency="USD",
                   amount="-25.00"),
        ]
        result = report(facts, details_)
        row = {row["currency"]: row
               for row in result["summary"]["by_currency"]}["USD"]
        self.assertEqual(row["ledger_total"], "30.00")
        self.assertEqual(row["external_total"], "25.00")
        self.assertEqual(row["difference"], "5.00")

    def test_base_currency_difference_accumulates_saved_gaps(self):
        facts = [
            fact("F1", voucher_id="V1", currency="USD", amount="10.00",
                 base_amount="73.00", rate={"rate": "7.2"}),
            fact("F2", currency="CNY", amount="40.00",
                 base_amount="40.00"),
        ]
        details_ = [
            detail("S1", voucher_ref="V1", currency="USD",
                   amount="10.00"),
        ]
        result = report(facts, details_)
        # F1 gap: 73.00 - 72.00 = 1.00; F2 has no external side and
        # contributes its saved base amount 40.00.
        self.assertEqual(result["summary"]["base_currency_difference"],
                         "41.00")

    def test_counts_reflect_every_category_once_per_record(self):
        facts = [
            fact("F1", voucher_ref="S1", amount="10.00"),       # matched
            fact("F2", voucher_ref="S2", amount="10.00"),       # amount
            fact("F3", amount="9.00"),                          # ext missing
        ]
        details_ = [
            detail("S1", amount="10.00"),
            detail("S2", amount="11.00"),
            detail("S4"),                                        # ledger miss
        ]
        counts = report(facts, details_)["summary"]["counts"]
        self.assertEqual(counts[DIFF_MATCHED], 1)
        self.assertEqual(counts[DIFF_AMOUNT_MISMATCH], 1)
        self.assertEqual(counts[DIFF_EXTERNAL_MISSING], 1)
        self.assertEqual(counts[DIFF_LEDGER_MISSING], 1)
        self.assertEqual(counts[DIFF_AMBIGUOUS], 0)

    def test_huge_amounts_keep_integer_precision(self):
        huge = "9007199254740993.00"
        facts = [fact("F1", date="2024-05-10", amount=huge)]
        details_ = [detail("S1", date="2024-05-10", amount=huge)]
        result = report(facts, details_)
        text = json.dumps(result, **JSON_KW)
        self.assertIn(huge, text)
        self.assertEqual(
            result["summary"]["by_currency"][0]["ledger_total"], huge)


# --------------------------------------------------------------------- #
# Fixed failure outcomes
# --------------------------------------------------------------------- #
class FailureOutcomeTests(unittest.TestCase):
    def test_duplicate_source_id_is_invalid_input_without_partial_report(self):
        result = report(
            [fact("F1")],
            [detail("A", amount="1.00"), detail("A", amount="2.00")])
        self.assertEqual(result["outcome"], OUTCOME_INVALID_INPUT)
        self.assertEqual(list(result), ["outcome", "reason"])
        self.assertIn("duplicate source_id", result["reason"])
        self.assertIn("'A'", result["reason"])
        self.assertNotIn("differences", result)

    def test_missing_book_set(self):
        # The target names book set OTHER, but the supplied book payload
        # belongs to B1: the requested target cannot be resolved.
        request = make_request([fact("F1")], [detail("S1")])
        request["book_set_id"] = "OTHER"
        result = build_reconciliation_report(request)
        self.assertEqual(result["outcome"], OUTCOME_TARGET_NOT_FOUND)
        self.assertEqual(result["target"], "book_set")
        self.assertEqual(result["book_set_id"], "OTHER")
        self.assertEqual(result["account_code"], "1001")

    def test_missing_account(self):
        result = report([fact("F1")], [detail("S1")], account="9999")
        self.assertEqual(result["outcome"], OUTCOME_TARGET_NOT_FOUND)
        self.assertEqual(result["target"], "account")

    def test_book_set_check_precedes_account_check(self):
        request = make_request([fact("F1")], [detail("S1")],
                               account="9999")
        request["book_set_id"] = "OTHER"
        result = build_reconciliation_report(request)
        self.assertEqual(result["target"], "book_set")

    def test_unclosed_historical_period_conflicts(self):
        periods = [{"period_id": "2024-05", "start_date": "2024-05-01",
                    "end_date": "2024-05-31", "closed": False}]
        result = report([fact("F1")], [detail("S1")], periods=periods)
        self.assertEqual(result["outcome"],
                         OUTCOME_PERIOD_STATUS_CONFLICT)
        self.assertEqual(result["period_id"], "2024-05")
        self.assertIs(result["closed"], False)

    def test_closed_period_reconciles(self):
        periods = [{"period_id": "2024-05", "start_date": "2024-05-01",
                    "end_date": "2024-05-31", "closed": True}]
        result = report([fact("F1")], [detail("S1")], periods=periods)
        self.assertEqual(result["outcome"], OUTCOME_RECONCILED)
        self.assertEqual(result["cutoff_period_id"], "2024-05")

    def test_cutoff_in_a_gap_or_open_future_is_allowed(self):
        periods = [{"period_id": "2024-05", "start_date": "2024-05-01",
                    "end_date": "2024-05-31", "closed": False}]
        result = report([], [], cutoff="2024-07-15", periods=periods)
        self.assertEqual(result["outcome"], OUTCOME_RECONCILED)
        self.assertIsNone(result["cutoff_period_id"])

    def test_invalid_request_shapes(self):
        good = make_request([fact("F1")], [detail("S1")])
        bad_cases = [
            None,
            "not-an-object",
            {k: v for k, v in good.items() if k != "cutoff"},
            {**good, "cutoff": "2024-05-32"},
            {**good, "cutoff": "2024/05/31"},
            {**good, "cutoff": None},
            {**good, "book_set_id": ""},
            {**good, "external_details": None},
            {**good, "external_details": "x"},
            {**good, "book": "x"},
            {**good, "book": {"facts": []}},
        ]
        for bad in bad_cases:
            with self.subTest(bad=str(bad)[:40]):
                result = build_reconciliation_report(bad)
                self.assertEqual(result["outcome"], OUTCOME_INVALID_INPUT)
                self.assertTrue(result["reason"])

    def test_invalid_base_currency_and_chart_are_invalid_input(self):
        good = make_request([fact("F1")], [detail("S1")])
        for patched in (
            {**good, "base_currency": "cny"},
            {**good, "chart_of_accounts": ["broken"]},
        ):
            with self.subTest():
                result = build_reconciliation_report(patched)
                self.assertEqual(result["outcome"], OUTCOME_INVALID_INPUT)

    def test_invalid_amounts_are_invalid_input(self):
        bad_fact_requests = [
            make_request([fact("F1", amount="1.005")], []),
            make_request([fact("F1", amount="-1.2.3")], []),
            make_request([fact("F1", amount=100)], []),
            make_request([fact("F1")], [detail("S1", amount="1,000.00")]),
            make_request([fact("F1")], [detail("S1", amount=5)]),
        ]
        for bad in bad_fact_requests:
            with self.subTest():
                self.assertEqual(
                    build_reconciliation_report(bad)["outcome"],
                    OUTCOME_INVALID_INPUT)

    def test_invalid_period_table_is_invalid_input(self):
        good = make_request([], [])
        bad_tables = [
            "x",
            [{"period_id": "P1", "start_date": "2024-05-01",
              "end_date": "2024-04-01", "closed": True}],
            [{"period_id": "P1", "start_date": "2024-05-01",
              "end_date": "2024-05-31", "closed": "yes"}],
            [{"period_id": "P1", "start_date": "2024-05-01",
              "end_date": "2024-05-31", "closed": True},
             {"period_id": "P1", "start_date": "2024-06-01",
              "end_date": "2024-06-30", "closed": True}],
            [{"period_id": "P2", "start_date": "2024-06-01",
              "end_date": "2024-06-30", "closed": True},
             {"period_id": "P1", "start_date": "2024-05-01",
              "end_date": "2024-05-31", "closed": True}],
        ]
        for table in bad_tables:
            with self.subTest():
                bad = {**good, "periods": table}
                self.assertEqual(
                    build_reconciliation_report(bad)["outcome"],
                    OUTCOME_INVALID_INPUT)

    def test_dangling_offset_and_reverser_references_are_invalid(self):
        f1 = fact("F1", offsets=[{"fact_id": "GHOST",
                                  "amount": "-1.00"}])
        self.assertEqual(report([f1], [])["outcome"],
                         OUTCOME_INVALID_INPUT)
        f2 = fact("F2", reversed_by=["GHOST"])
        self.assertEqual(report([f2], [])["outcome"],
                         OUTCOME_INVALID_INPUT)

    def test_validation_boundary_precedes_target_existence(self):
        # A duplicate source id must be invalid_input even though the
        # named book set also does not exist.
        request = make_request(
            [fact("F1")], [detail("A"), detail("A")])
        request["book_set_id"] = "OTHER"
        result = build_reconciliation_report(request)
        self.assertEqual(result["outcome"], OUTCOME_INVALID_INPUT)
        # A broken chart masks the missing-account target too.
        bad = make_request([fact("F1")], [detail("S1")], account="9999",
                           chart=[{"nope": True}])
        self.assertEqual(build_reconciliation_report(bad)["outcome"],
                         OUTCOME_INVALID_INPUT)


# --------------------------------------------------------------------- #
# Determinism: ordering, hash seed, container freshness and immutation
# --------------------------------------------------------------------- #
def rich_fixture():
    facts = [
        fact("F1", voucher_id="V1", line_no=1, voucher_ref="S1",
             date="2024-05-10", currency="CNY", amount="100.00",
             base_amount="100.00"),
        fact("F2", date="2024-05-11", currency="USD", amount="-50.00",
             base_amount="-360.00",
             rate={"rate": "7.2", "rate_date": "2024-05-11",
                   "rate_source": "r1"}),
        fact("F3", voucher_id="V3", date="2024-05-12", currency="USD",
             amount="20.00", base_amount="145.00",
             rate={"rate": "7.2"}),
        fact("F4", date="2024-05-13", currency="CNY", amount="9.00"),
        fact("F5", date="2024-05-14", currency="CNY", amount="7.00"),
        fact("F6", date="2024-05-14", currency="CNY", amount="7.00"),
        fact("F7", account="2202", date="2024-05-14", amount="777.00"),
        fact("F8", date="2024-06-20", amount="1.00"),
    ]
    details_ = [
        detail("S1", date="2024-05-10", currency="CNY", amount="100.00"),
        detail("S2", date="2024-05-11", currency="USD", amount="-55.00"),
        detail("S3", voucher_ref="V3", date="2024-05-12",
               currency="USD", amount="20.00"),
        detail("S5", date="2024-05-14", currency="CNY", amount="7.00"),
        detail("S6", date="2024-05-14", currency="CNY", amount="7.00"),
        detail("S9", date="2024-05-15", currency="CNY", amount="3.00"),
    ]
    return facts, details_


class DeterminismTests(unittest.TestCase):
    def setUp(self):
        facts, details_ = rich_fixture()
        self.facts = facts
        self.details = details_
        self.reference = report(copy.deepcopy(facts), copy.deepcopy(details_))

    def test_permutations_are_value_and_byte_equal(self):
        facts, details_ = self.facts, self.details
        ref_text = json.dumps(self.reference, **JSON_KW)
        # A representative family of orderings (an exhaustive factorial
        # would be tens of millions of pairs); every permutation used is a
        # true reordering, and each side is reordered independently.
        fact_orders = [
            tuple(range(len(facts))),
            tuple(reversed(range(len(facts)))),
        ]

        def rotations(n):
            return [tuple(range(k, n)) + tuple(range(k)) for k in range(n)]

        fact_orders += rotations(len(facts))
        detail_orders = [
            tuple(range(len(details_))),
            tuple(reversed(range(len(details_)))),
        ] + rotations(len(details_))
        for fact_order in fact_orders:
            for detail_order in detail_orders:
                self.assertEqual(sorted(fact_order), list(range(len(facts))))
                permuted_facts = [copy.deepcopy(facts[i])
                                  for i in fact_order]
                permuted_details = [copy.deepcopy(details_[i])
                                    for i in detail_order]
                result = report(permuted_facts, permuted_details)
                self.assertEqual(result, self.reference)
                self.assertEqual(json.dumps(result, **JSON_KW), ref_text)

    def test_section_rows_use_stable_business_keys(self):
        result = self.reference
        # Only S1-F1 matches cleanly; rows are keyed by external id.
        self.assertEqual(
            [i["external"]["source_id"]
             for i in section(result, DIFF_MATCHED)], ["S1"])
        # External-missing rows sort by (business date, fact id).
        self.assertEqual(
            [i["ledger"]["fact_id"]
             for i in section(result, DIFF_EXTERNAL_MISSING)],
            ["F2", "F4"])
        # Ledger-missing rows sort by external source id.
        self.assertEqual(
            [i["external"]["source_id"]
             for i in section(result, DIFF_LEDGER_MISSING)],
            ["S2", "S9"])
        ambiguous = section(result, DIFF_AMBIGUOUS)
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual([d["source_id"] for d in ambiguous[0]["external"]],
                         ["S5", "S6"])
        self.assertEqual([d["fact_id"] for d in ambiguous[0]["ledger"]],
                         ["F5", "F6"])
        # summary currencies sorted by code
        self.assertEqual(
            [row["currency"]
             for row in result["summary"]["by_currency"]],
            ["CNY", "USD"])

    def test_reversed_dict_insertion_order_is_invisible(self):
        def reverse_keys(value):
            if isinstance(value, dict):
                return {k: reverse_keys(v)
                        for k, v in reversed(list(value.items()))}
            if isinstance(value, list):
                return [reverse_keys(v) for v in value]
            return value

        request = make_request(copy.deepcopy(self.facts),
                               copy.deepcopy(self.details))
        shuffled = reverse_keys(request)
        result = build_reconciliation_report(shuffled)
        self.assertEqual(json.dumps(result, **JSON_KW),
                         json.dumps(self.reference, **JSON_KW))

    def test_repeated_calls_return_disjoint_fresh_containers(self):
        first = report(copy.deepcopy(self.facts), copy.deepcopy(self.details))
        second = report(copy.deepcopy(self.facts), copy.deepcopy(self.details))
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertTrue(mutable_ids(first).isdisjoint(mutable_ids(second)))

    def test_result_shares_no_containers_with_inputs(self):
        request = make_request(copy.deepcopy(self.facts),
                               copy.deepcopy(self.details))
        result = build_reconciliation_report(copy.deepcopy(request))
        self.assertTrue(
            mutable_ids(result).isdisjoint(
                mutable_ids(request["chart_of_accounts"])
                | mutable_ids(request["book"])
                | mutable_ids(request["external_details"])))

    def test_corrupting_a_result_never_reaches_a_later_call(self):
        first = report(copy.deepcopy(self.facts), copy.deepcopy(self.details))
        snapshot = copy.deepcopy(first)
        first["differences"][DIFF_MATCHED][0]["ledger"]["amount"] = "0.00"
        first["summary"]["by_currency"].append("junk")
        second = report(copy.deepcopy(self.facts), copy.deepcopy(self.details))
        self.assertEqual(second, snapshot)

    def test_inputs_are_not_mutated(self):
        request = make_request(copy.deepcopy(self.facts),
                               copy.deepcopy(self.details))
        snapshot = copy.deepcopy(request)
        build_reconciliation_report(request)
        self.assertEqual(request, snapshot)

    def test_bytes_identical_across_hash_seeds(self):
        request = make_request(*rich_fixture())
        encoded = json.dumps(request, ensure_ascii=False)
        child = (
            "import json, os, sys;"
            "sys.path.insert(0, os.environ['LEDGER_ENGINE_ROOT']);"
            "from ledger_engine import build_reconciliation_report as b;"
            "req=json.loads(sys.stdin.buffer.read().decode('utf-8'));"
            "out=b(req);"
            "sys.stdout.buffer.write(json.dumps(out, sort_keys=True,"
            "ensure_ascii=False,separators=(',',':')).encode('utf-8'))"
        )
        env_base = dict(os.environ)
        env_base["LEDGER_ENGINE_ROOT"] = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))

        def run(seed):
            env = dict(env_base)
            env["PYTHONHASHSEED"] = str(seed)
            env["PYTHONUTF8"] = "1"
            completed = subprocess.run(
                [sys.executable, "-c", child], input=encoded.encode("utf-8"),
                env=env, capture_output=True, check=True)
            return completed.stdout

        reference = run(0)
        for seed in (1, 2, 7, 12345, "random"):
            with self.subTest(seed=seed):
                self.assertEqual(run(seed), reference)
        self.assertEqual(reference,
                         json.dumps(self.reference, **JSON_KW).encode("utf-8"))


# --------------------------------------------------------------------- #
# Closed-period historical stability against later ledger activity
# --------------------------------------------------------------------- #
class HistoricalStabilityTests(unittest.TestCase):
    def test_later_vouchers_rates_and_reversals_leave_report_unchanged(self):
        periods = [
            {"period_id": "2024-05", "start_date": "2024-05-01",
             "end_date": "2024-05-31", "closed": True},
            {"period_id": "2024-06", "start_date": "2024-06-01",
             "end_date": "2024-06-30", "closed": False},
        ]
        may_facts = [
            fact("F1", voucher_ref="S1", date="2024-05-10",
                 currency="USD", amount="10.00", base_amount="72.00",
                 rate={"rate": "7.2", "rate_date": "2024-05-10"}),
            fact("F2", date="2024-05-11", currency="CNY", amount="5.00"),
        ]
        may_details = [
            detail("S1", date="2024-05-10", currency="USD", amount="10.00"),
        ]
        before = report(copy.deepcopy(may_facts), copy.deepcopy(may_details),
                        cutoff="2024-05-31", periods=periods)

        # Later activity: a new June voucher, a new rate record, and a
        # June reversal of the May fact -- all after the cutoff.
        later_facts = may_facts + [
            fact("F3", date="2024-06-02", currency="USD", amount="10.00",
                 base_amount="74.00",
                 rate={"rate": "7.4", "rate_date": "2024-06-02"}),
            {**fact("F4", date="2024-06-03", currency="USD",
                    amount="-10.00"), "reverses_hint": True},
        ]
        later_facts[0] = {**may_facts[0], "reversed_by": ["F4"]}
        after = report(copy.deepcopy(later_facts),
                       copy.deepcopy(may_details),
                       cutoff="2024-05-31", periods=periods)
        self.assertEqual(json.dumps(after, **JSON_KW),
                         json.dumps(before, **JSON_KW))
        # The May fact survives (its reverser is dated after the cutoff)
        # and still carries the historical 7.2 rate.
        matched = section(after, DIFF_MATCHED)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["ledger"]["rate"]["rate"], "7.2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
