"""Regression tests for ``ledger_engine.reverse_vouchers``.

The batch-reversal entry point shares the validation pipeline with
``normalize_vouchers``/``post_vouchers``/``build_trial_balance`` and adds
only deterministic reversal assembly on top.  These tests lock the added
contract without changing any existing behavior:

* exactly one reversing voucher per source, in source order, id fixed to
  ``"REV:" + source id`` (literal prefix, never folded), date replaced by
  the one reversal date, currency/accounts/summaries/entry order kept;
* every line's debit and credit swap sides in integer cents while line
  numbers restart at one, and amounts/totals keep the two-decimal string
  contract with the same voucher/entry fields and key order;
* the result re-normalizes to an equal container and, posted with the
  sources, cancels every per-account turnover to a zero ending net;
* the complete source-batch validation sequence runs first with its exact
  leaf exceptions and first-failure rule; only a fully valid batch lets
  the reversal-date checks (calendar validity, then not earlier than any
  source date, both ``VoucherFormatError``) run;
* no mutation of the chart, vouchers or nested containers; repeated
  calls return equal, byte-reproducible, mutually disjoint containers.

Standard library only; CPython 3.10+.
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
    UnsupportedCurrencyError,
    UnbalancedVoucherError,
    UnknownAccountError,
    VoucherFormatError,
    build_trial_balance,
    normalize_vouchers,
    post_vouchers,
    reverse_vouchers,
)
from ledger_engine.vouchers import reverse_vouchers as package_reverse_vouchers

BASE = "CNY"
JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}

VOUCHER_KEYS = ["voucher_id", "date", "currency", "entries",
                "debit_total", "credit_total"]
ENTRY_KEYS = ["line_no", "account_code", "account_name", "normal_side",
              "summary", "debit", "credit"]


# --------------------------------------------------------------------- #
# Fresh-object factories (same fixtures as the other public-contract suites).
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


def make_voucher(voucher_id="V-2024-001", date_text="2024-01-10",
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
            "V-2024-002", "2024-02-29",
            entries=[
                make_entry("1001", "支付欠款", debit="1234.5",
                           credit="0.00"),
                make_entry("2202", "冲减应付", debit="0",
                           credit="1200.00"),
                make_entry("6601", "费用调整", debit="0.00",
                           credit="34.5"),
            ],
        ),
    ]


REVERSAL_DATE = "2024-03-01"


class ReverseTestCase(unittest.TestCase):
    def assert_reverses(self, chart, vouchers, reversal_date, expected):
        result = reverse_vouchers(BASE, chart, vouchers, reversal_date)
        self.assertEqual(result, expected)
        return result

    def assert_rejects(self, expected_exc, chart, vouchers, reversal_date,
                       base=BASE):
        """Call must raise exactly expected_exc and leave all inputs alone."""
        snapshot = copy.deepcopy((base, chart, vouchers, reversal_date))
        with self.assertRaises(expected_exc) as caught:
            reverse_vouchers(base, chart, vouchers, reversal_date)
        self.assertIs(type(caught.exception), expected_exc,
                      "unique public category must be the leaf class")
        self.assertEqual((base, chart, vouchers, reversal_date), snapshot,
                         "failed reversal must not mutate its inputs")
        return caught.exception


# --------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------- #
class PublicSurfaceTests(unittest.TestCase):
    def test_exported_from_top_level_and_vouchers_package(self):
        import ledger_engine
        import ledger_engine.vouchers as vouchers_pkg

        self.assertIs(ledger_engine.reverse_vouchers, reverse_vouchers)
        self.assertIs(package_reverse_vouchers, reverse_vouchers)
        self.assertIn("reverse_vouchers", ledger_engine.__all__)
        self.assertIn("reverse_vouchers", vouchers_pkg.__all__)


# --------------------------------------------------------------------- #
# Success path: shape of the reversal
# --------------------------------------------------------------------- #
class ReversalShapeTests(ReverseTestCase):
    def test_one_reversal_per_source_in_source_order(self):
        result = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        self.assertEqual(len(result), 2)
        self.assertEqual([v["voucher_id"] for v in result],
                         ["REV:V-2024-001", "REV:V-2024-002"])

    def test_reversal_id_is_literal_prefix_even_when_source_has_it(self):
        # No rewriting, folding or de-duplication of an existing prefix.
        vouchers = [
            make_voucher("REV:V-1", "2024-01-01"),
            make_voucher("V-1", "2024-01-02"),
        ]
        result = reverse_vouchers(BASE, make_chart(), vouchers, "2024-02-01")
        self.assertEqual([v["voucher_id"] for v in result],
                         ["REV:REV:V-1", "REV:V-1"])

    def test_reversing_twice_stacks_the_prefix_again(self):
        once = reverse_vouchers(BASE, make_chart(), make_batch(),
                                REVERSAL_DATE)
        twice = reverse_vouchers(BASE, make_chart(), once, "2024-03-02")
        self.assertEqual([v["voucher_id"] for v in twice],
                         ["REV:REV:V-2024-001", "REV:REV:V-2024-002"])

    def test_date_is_replaced_by_the_single_reversal_date(self):
        result = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        self.assertEqual([v["date"] for v in result],
                         [REVERSAL_DATE, REVERSAL_DATE])

    def test_same_day_reversal_is_accepted(self):
        # "Not earlier than" is an inclusive boundary: equality is fine.
        result = reverse_vouchers(BASE, make_chart(),
                                  [make_voucher("V-1", "2024-01-10")],
                                  "2024-01-10")
        self.assertEqual(result[0]["date"], "2024-01-10")

    def test_currency_accounts_summaries_and_entry_order_are_kept(self):
        sources = make_batch()
        result = reverse_vouchers(BASE, make_chart(), sources, REVERSAL_DATE)
        for source, reversal in zip(sources, result):
            self.assertEqual(reversal["currency"], source["currency"])
            self.assertEqual(
                [e["account_code"] for e in reversal["entries"]],
                [e["account_code"] for e in source["entries"]],
            )
            self.assertEqual(
                [e["summary"] for e in reversal["entries"]],
                [e["summary"] for e in source["entries"]],
            )

    def test_account_name_and_normal_side_come_from_chart_snapshot(self):
        chart = make_chart()
        by_code = {a["code"]: a for a in chart}
        result = reverse_vouchers(BASE, chart, make_batch(), REVERSAL_DATE)
        for voucher in result:
            for entry in voucher["entries"]:
                source = by_code[entry["account_code"]]
                self.assertEqual(entry["account_name"], source["name"])
                self.assertEqual(entry["normal_side"],
                                 source["normal_side"])

    def test_debit_and_credit_swap_sides_on_every_line(self):
        normalized = normalize_vouchers(BASE, make_chart(), make_batch())
        result = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        for source, reversal in zip(normalized, result):
            for src_line, rev_line in zip(source["entries"],
                                          reversal["entries"]):
                self.assertEqual(rev_line["debit"], src_line["credit"])
                self.assertEqual(rev_line["credit"], src_line["debit"])

    def test_line_numbers_restart_at_one_and_stay_sequential(self):
        result = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        for voucher in result:
            self.assertEqual([e["line_no"] for e in voucher["entries"]],
                             list(range(1, len(voucher["entries"]) + 1)))

    def test_amounts_and_totals_keep_two_decimal_rendering_and_swap(self):
        result = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        first, second = result
        # Line amounts are canonical two-place strings, sides swapped.
        self.assertEqual(first["entries"][0]["debit"], "0.00")
        self.assertEqual(first["entries"][0]["credit"], "100.00")
        self.assertEqual(first["entries"][1]["debit"], "100.00")
        self.assertEqual(first["entries"][1]["credit"], "0.00")
        self.assertEqual(second["entries"][0]["debit"], "0.00")
        self.assertEqual(second["entries"][0]["credit"], "1234.50")
        self.assertEqual(second["entries"][1]["debit"], "1200.00")
        self.assertEqual(second["entries"][1]["credit"], "0.00")
        self.assertEqual(second["entries"][2]["debit"], "34.50")
        self.assertEqual(second["entries"][2]["credit"], "0.00")
        # Balanced sources keep balanced totals after the swap.
        for voucher in result:
            self.assertEqual(voucher["debit_total"], voucher["credit_total"])
        self.assertEqual(first["debit_total"], "100.00")
        self.assertEqual(first["credit_total"], "100.00")
        self.assertEqual(second["debit_total"], "1234.50")
        self.assertEqual(second["credit_total"], "1234.50")

    def test_field_and_key_insertion_order_match_normalization(self):
        result = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        for voucher in result:
            self.assertEqual(list(voucher.keys()), VOUCHER_KEYS)
            for entry in voucher["entries"]:
                self.assertEqual(list(entry.keys()), ENTRY_KEYS)

    def test_huge_amounts_keep_full_integer_precision_through_swap(self):
        vouchers = [
            make_voucher(
                "V-BIG", "2024-01-01",
                entries=[
                    make_entry("6601", "大额",
                               debit="99999999999999999999.99",
                               credit="0.00"),
                    make_entry("2202", "对方", debit="0.00",
                               credit="99999999999999999999.99"),
                ],
            )
        ]
        result = reverse_vouchers(BASE, make_chart(), vouchers,
                                  "2024-02-01")
        self.assertEqual(result[0]["entries"][0]["debit"], "0.00")
        self.assertEqual(result[0]["entries"][0]["credit"],
                         "99999999999999999999.99")
        self.assertEqual(result[0]["entries"][1]["debit"],
                         "99999999999999999999.99")
        self.assertEqual(result[0]["entries"][1]["credit"], "0.00")


# --------------------------------------------------------------------- #
# Compatibility with the other public entry points
# --------------------------------------------------------------------- #
class DownstreamCompatibilityTests(ReverseTestCase):
    def test_reversal_renormalizes_into_an_equal_container(self):
        result = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        self.assertEqual(
            normalize_vouchers(BASE, make_chart(),
                               copy.deepcopy(result)),
            result,
        )

    def test_sources_and_reversals_cancel_in_trial_balance(self):
        chart = make_chart()
        combined = make_batch() + reverse_vouchers(
            BASE, chart, make_batch(), REVERSAL_DATE)
        trial = build_trial_balance(BASE, chart, combined)
        for row in trial["accounts"]:
            self.assertEqual(row["debit_turnover"], row["credit_turnover"],
                             row)
            self.assertEqual(row["ending_debit"], "0.00", row)
            self.assertEqual(row["ending_credit"], "0.00", row)
        totals = trial["totals"]
        self.assertEqual(totals["ending_debit"], "0.00")
        self.assertEqual(totals["ending_credit"], "0.00")
        self.assertIs(totals["turnover_balanced"], True)
        self.assertIs(totals["ending_balanced"], True)

    def test_sources_and_reversals_cancel_in_posted_balances(self):
        chart = make_chart()
        combined = make_batch() + reverse_vouchers(
            BASE, chart, make_batch(), REVERSAL_DATE)
        posted = post_vouchers(BASE, chart, combined)
        for row in posted["accounts"]:
            self.assertEqual(row["debit_turnover"], row["credit_turnover"],
                             row)
            self.assertEqual(row["ending_balance"], "0.00", row)
            # A zero net keeps the chart's normal side, never flips it.
            self.assertEqual(row["ending_side"], row["normal_side"], row)

    def test_journal_pairs_each_reversed_line_with_swapped_sides(self):
        chart = make_chart()
        sources = make_batch()
        combined = sources + reverse_vouchers(BASE, chart, sources,
                                              REVERSAL_DATE)
        posted = post_vouchers(BASE, chart, combined)
        journal = posted["journal"]
        # Half the rows are sources, half reversals; same (account, line)
        # pairing carries the opposite amounts.
        source_rows = [r for r in journal
                       if not r["voucher_id"].startswith("REV:")]
        reversal_rows = [r for r in journal
                         if r["voucher_id"].startswith("REV:")]
        self.assertEqual(len(source_rows), len(reversal_rows))
        for src, rev in zip(source_rows, reversal_rows):
            self.assertEqual(rev["voucher_id"], "REV:" + src["voucher_id"])
            self.assertEqual(rev["line_no"], src["line_no"])
            self.assertEqual(rev["account_code"], src["account_code"])
            self.assertEqual(rev["summary"], src["summary"])
            self.assertEqual(rev["debit"], src["credit"])
            self.assertEqual(rev["credit"], src["debit"])

    def test_reversal_alone_has_equal_and_opposite_turnovers(self):
        chart = make_chart()
        sources = make_batch()
        source_trial = build_trial_balance(BASE, chart, sources)
        reversal_trial = build_trial_balance(
            BASE, chart,
            reverse_vouchers(BASE, chart, sources, REVERSAL_DATE))
        for src_row, rev_row in zip(source_trial["accounts"],
                                    reversal_trial["accounts"]):
            self.assertEqual(rev_row["debit_turnover"],
                             src_row["credit_turnover"])
            self.assertEqual(rev_row["credit_turnover"],
                             src_row["debit_turnover"])

    def test_reversal_output_is_json_serializable(self):
        result = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        encoded = json.dumps(result)  # default kwargs must work
        self.assertEqual(json.loads(encoded), result)


# --------------------------------------------------------------------- #
# Empty batch
# --------------------------------------------------------------------- #
class EmptyBatchTests(ReverseTestCase):
    def test_empty_batch_returns_empty_list(self):
        for reversal_date in ("2024-03-01", "2000-01-01", "1999-12-31"):
            with self.subTest(reversal_date=reversal_date):
                self.assertEqual(
                    reverse_vouchers(BASE, make_chart(), [], reversal_date),
                    [],
                )

    def test_empty_batch_still_validates_the_reversal_calendar_date(self):
        # No source dates means the ordering rule vacuously passes, but an
        # impossible reversal date is still a format error.
        self.assert_rejects(VoucherFormatError, make_chart(), [],
                            "2023-02-29")
        self.assert_rejects(VoucherFormatError, make_chart(), [], None)


# --------------------------------------------------------------------- #
# Reversal-date boundaries
# --------------------------------------------------------------------- #
class ReversalDateTests(ReverseTestCase):
    def test_not_a_calendar_date_rejected(self):
        for value in ("2023-02-29", "2100-02-29", "2024-13-01",
                      "2024-00-10", "2024-1-01", "2024/03/01", "20240301",
                      "", None, 20240301, True, False, ["2024-03-01"]):
            with self.subTest(value=value):
                self.assert_rejects(VoucherFormatError, make_chart(),
                                    make_batch(), value)

    def test_earlier_than_a_source_date_rejected(self):
        self.assert_rejects(VoucherFormatError, make_chart(), make_batch(),
                            "2024-01-09")
        self.assert_rejects(VoucherFormatError, make_chart(), make_batch(),
                            "2024-02-28")

    def test_first_violating_voucher_in_batch_order_is_named(self):
        # First voucher is dated 2024-01-10 and validates; the second
        # (2024-02-29) is the first one the date precedes.
        exc = self.assert_rejects(VoucherFormatError, make_chart(),
                                  make_batch(), "2024-02-28")
        message = str(exc)
        self.assertIn("V-2024-002", message)
        self.assertIn("2024-02-28", message)
        self.assertIn("2024-02-29", message)
        self.assertNotIn("V-2024-001", message)


# --------------------------------------------------------------------- #
# Source validation stays exactly the shared pipeline
# --------------------------------------------------------------------- #
class SourceValidationTests(ReverseTestCase):
    def test_chart_problem_masks_everything_else(self):
        self.assert_rejects(ChartOfAccountsError, ["broken"], make_batch(),
                            REVERSAL_DATE)
        # Even a nonsensical reversal date must not mask a broken chart.
        self.assert_rejects(ChartOfAccountsError, ["broken"], make_batch(),
                            "not-a-date")

    def test_bad_base_currency_masks_bad_reversal_date(self):
        self.assert_rejects(ChartOfAccountsError, make_chart(), make_batch(),
                            "not-a-date", base="bad")

    def test_batch_must_be_a_list(self):
        for payload in (None, {}, tuple(), "batch", make_voucher()):
            with self.subTest(payload=type(payload).__name__):
                self.assert_rejects(VoucherFormatError, make_chart(),
                                    payload, REVERSAL_DATE)

    def test_structural_source_defect_reported(self):
        bad = [make_voucher(entries=[make_entry()])]  # one entry only
        self.assert_rejects(VoucherFormatError, make_chart(), bad,
                            REVERSAL_DATE)

    def test_duplicate_source_id_reported(self):
        vouchers = [make_voucher(), make_voucher()]
        self.assert_rejects(DuplicateVoucherError, make_chart(), vouchers,
                            REVERSAL_DATE)

    def test_exact_source_id_semantics_keep_reversal_ids_distinct(self):
        # Case/space-distinct source ids survive unchanged after prefixing;
        # generated ids must not be folded onto each other.
        vouchers = [
            make_voucher("V-001", "2024-01-01"),
            make_voucher("v-001", "2024-01-02"),
            make_voucher(" V-001", "2024-01-03"),
        ]
        result = reverse_vouchers(BASE, make_chart(), vouchers,
                                  "2024-02-01")
        self.assertEqual(
            [v["voucher_id"] for v in result],
            ["REV:V-001", "REV:v-001", "REV: V-001"],
        )

    def test_foreign_currency_reported(self):
        vouchers = [make_voucher(currency="USD")]
        self.assert_rejects(UnsupportedCurrencyError, make_chart(),
                            vouchers, REVERSAL_DATE)

    def test_unknown_account_reported(self):
        vouchers = [make_voucher(entries=[
            make_entry("9999", "未知", debit="1.00"),
            make_entry("2202", "贷方", credit="1.00"),
        ])]
        self.assert_rejects(UnknownAccountError, make_chart(), vouchers,
                            REVERSAL_DATE)

    def test_inactive_account_reported(self):
        vouchers = [make_voucher(entries=[
            make_entry("4001", "停用", debit="1.00"),
            make_entry("2202", "贷方", credit="1.00"),
        ])]
        self.assert_rejects(InactiveAccountError, make_chart(), vouchers,
                            REVERSAL_DATE)

    def test_bad_amount_reported(self):
        vouchers = [make_voucher(entries=[
            make_entry("6601", "金额坏", debit="1,000.00"),
            make_entry("2202", "贷方", credit="1000.00"),
        ])]
        self.assert_rejects(InvalidEntryAmountError, make_chart(), vouchers,
                            REVERSAL_DATE)

    def test_unbalanced_source_reported(self):
        vouchers = [make_voucher(entries=[
            make_entry("6601", "借方", debit="100.00"),
            make_entry("2202", "贷方", credit="99.99"),
        ])]
        self.assert_rejects(UnbalancedVoucherError, make_chart(), vouchers,
                            REVERSAL_DATE)

    def test_source_problem_always_masks_reversal_date_problem(self):
        # Whatever is wrong with the reversal date, an invalid source batch
        # reports its own first leaf exception instead.
        vouchers = [make_voucher(entries=[
            make_entry("6601", "借方", debit="100.00"),
            make_entry("2202", "贷方", credit="99.99"),
        ])]
        for bad_date in ("not-a-date", None, "2023-12-31", 0):
            with self.subTest(bad_date=bad_date):
                self.assert_rejects(UnbalancedVoucherError, make_chart(),
                                    vouchers, bad_date)

    def test_validation_stops_at_first_invalid_source(self):
        vouchers = [
            make_voucher("V-OK", "2024-01-01"),
            make_voucher("V-BAD", "2024-01-02", currency="USD"),
        ]
        self.assert_rejects(UnsupportedCurrencyError, make_chart(),
                            vouchers, "2024-01-01")

    def test_invalid_source_produces_no_partial_result(self):
        # The call raises; there is no result object to observe at all.
        with self.assertRaises(UnbalancedVoucherError):
            reverse_vouchers(BASE, make_chart(),
                             [make_voucher(entries=[
                                 make_entry("6601", "借方", debit="1.00"),
                                 make_entry("2202", "贷方", credit="0.99"),
                             ])],
                             REVERSAL_DATE)


# --------------------------------------------------------------------- #
# No mutation, reproducibility, container independence
# --------------------------------------------------------------------- #
class InvarianceTests(ReverseTestCase):
    def test_inputs_not_mutated_on_success(self):
        chart = make_chart()
        vouchers = make_batch()
        snapshot = copy.deepcopy((BASE, chart, vouchers, REVERSAL_DATE))
        reverse_vouchers(BASE, chart, vouchers, REVERSAL_DATE)
        self.assertEqual((BASE, chart, vouchers, REVERSAL_DATE), snapshot)
        # Raw source amount spellings and shape stay exactly as supplied.
        self.assertEqual(vouchers[0]["entries"][0]["debit"], "100")
        self.assertEqual(vouchers[0]["entries"][0]["credit"], "00")
        self.assertNotIn("line_no", vouchers[0]["entries"][0])
        self.assertNotIn("debit_total", vouchers[0])

    def test_raw_chart_mutation_after_the_call_cannot_reach_result(self):
        chart = make_chart()
        result = reverse_vouchers(BASE, chart, make_batch(), REVERSAL_DATE)
        chart[0]["name"] = "被篡改的名称"
        chart[0]["active"] = False
        chart.append({"code": "9999", "name": "事后新增",
                      "normal_side": "credit", "active": True})
        names = {e["account_name"] for v in result for e in v["entries"]}
        self.assertIn("库存现金", names)
        self.assertNotIn("被篡改的名称", names)

    def test_source_mutation_after_the_call_cannot_reach_result(self):
        vouchers = make_batch()
        result = reverse_vouchers(BASE, make_chart(), vouchers,
                                  REVERSAL_DATE)
        vouchers[0]["voucher_id"] = "CHANGED"
        vouchers[0]["entries"][0]["summary"] = "被篡改"
        vouchers.append(make_voucher("V-LATER", "2024-01-01"))
        self.assertEqual(result[0]["voucher_id"], "REV:V-2024-001")
        self.assertEqual(result[0]["entries"][0]["summary"], "购办公用品")
        self.assertEqual(len(result), 2)

    def test_repeated_calls_return_equal_disjoint_containers(self):
        first = reverse_vouchers(BASE, make_chart(), make_batch(),
                                 REVERSAL_DATE)
        second = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        for voucher_a, voucher_b in zip(first, second):
            self.assertIsNot(voucher_a, voucher_b)
            self.assertIsNot(voucher_a["entries"], voucher_b["entries"])
            for entry_a, entry_b in zip(voucher_a["entries"],
                                        voucher_b["entries"]):
                self.assertIsNot(entry_a, entry_b)

    def test_corrupting_one_result_never_reaches_a_later_call(self):
        first = reverse_vouchers(BASE, make_chart(), make_batch(),
                                 REVERSAL_DATE)
        first[0]["voucher_id"] = "CORRUPT"
        first[0]["entries"][0]["debit"] = "999.99"
        first.append({"junk": True})
        second = reverse_vouchers(BASE, make_chart(), make_batch(),
                                  REVERSAL_DATE)
        self.assertEqual(second[0]["voucher_id"], "REV:V-2024-001")
        self.assertEqual(second[0]["entries"][0]["debit"], "0.00")
        self.assertEqual(len(second), 2)

    def test_byte_identical_json_across_repeated_calls(self):
        text_a = json.dumps(
            reverse_vouchers(BASE, make_chart(), make_batch(),
                             REVERSAL_DATE), **JSON_KW)
        text_b = json.dumps(
            reverse_vouchers(BASE, make_chart(), make_batch(),
                             REVERSAL_DATE), **JSON_KW)
        self.assertEqual(text_a, text_b)
        self.assertEqual(text_a.encode("utf-8"), text_b.encode("utf-8"))

    def test_failure_leaves_nested_source_containers_untouched(self):
        chart = make_chart()
        vouchers = [make_voucher(entries=[
            make_entry("8888", "未知", debit="bad"),
            make_entry("2202", "贷方", credit="1.00"),
        ])]
        snapshot = copy.deepcopy((chart, vouchers))
        with self.assertRaises(UnknownAccountError):
            reverse_vouchers(BASE, chart, vouchers, REVERSAL_DATE)
        self.assertEqual((chart, vouchers), snapshot)
        # A bad reversal date is checked after sources; failure there must
        # not touch the sources either.
        good_batch = make_batch()
        good_snapshot = copy.deepcopy(good_batch)
        with self.assertRaises(VoucherFormatError):
            reverse_vouchers(BASE, make_chart(), good_batch, "2020-01-01")
        self.assertEqual(good_batch, good_snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
