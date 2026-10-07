"""Regression tests for book-to-external difference localization.

These tests lock the reconciliation contract added on top of the
unchanged voucher baseline:

* seven categories -- exact match, missing on the books, missing
  externally, original-amount mismatch, currency mismatch, base-
  conversion mismatch and ambiguous match -- with two-sided record
  ids, compared amounts, recorded-rate evidence and voucher refs;
* reference identity (echoed source id, then voucher reference) takes
  precedence over the ``(account, currency, amount, date)`` attribute
  fallback; non-unique candidates are always ambiguity groups and are
  never resolved by input order; dangling identifiers forbid attribute
  fallback;
* read-only scope: same account, facts effective by the cutoff and not
  cancelled by a reversal effective by the cutoff; future facts, future
  reversals and other accounts never enter the report;
* fixed boundaries: duplicate external source ids (and malformed
  payloads) -> invalid input with no partial report; unknown book set
  or account -> target not found; unclosed historical period -> period
  status conflict; the empty snapshot is valid;
* per-currency original totals (never summed across currencies), base
  totals only in the single base currency; exact integer minor-unit
  arithmetic including zero-decimal currencies and half-up rate
  rounding;
* byte-identical serialization under arbitrary input permutations, hash
  seeds and repeated calls, with fresh, input-disjoint containers;
* closed-period historical reports stay identical after later vouchers,
  rates and reversals are added.

Standard library only; CPython 3.10+.
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import subprocess
import sys
import unittest

from ledger_engine import (
    InvalidReconciliationInputError,
    LedgerEngineError,
    PeriodStatusConflictError,
    TargetNotFoundError,
    build_reconciliation_report,
)
from ledger_engine.reconciliation import money
from ledger_engine.reconciliation.matching import external_effective_base
from ledger_engine.reconciliation.model import parse_external_lines

JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}

BASE = "CNY"
BOOK = "BS-2024"
ACCOUNT = "1002"
CUTOFF = "2024-03-31"


# --------------------------------------------------------------------- #
# Fixture factories -- fresh containers on every call.
# --------------------------------------------------------------------- #
def make_accounts():
    return [
        {"code": "1002", "name": "银行存款", "normal_side": "debit",
         "active": True},
        {"code": "6601", "name": "管理费用", "normal_side": "debit",
         "active": True},
    ]


def make_periods():
    return [
        {"start": "2024-01-01", "closed": True},
        {"start": "2024-02-01", "closed": True},
        {"start": "2024-03-01", "closed": True},
        {"start": "2024-04-01", "closed": False},
    ]


def fact(entry_id, voucher_id, day, currency, original, base, *,
         line_no=1, account=ACCOUNT, source_ref=None, recorded_rate=None,
         month="02"):
    row = {
        "entry_id": entry_id,
        "voucher_id": voucher_id,
        "line_no": line_no,
        "date": f"2024-{month}-{day:02d}",
        "account_code": account,
        "currency": currency,
        "original_amount": original,
        "base_amount": base,
    }
    if source_ref is not None:
        row["source_ref"] = source_ref
    if recorded_rate is not None:
        row["recorded_rate"] = recorded_rate
    return row


def line(source_id, day, currency, amount, *, voucher_ref=None,
         base_amount=None, recorded_rate=None, month="02"):
    row = {
        "source_id": source_id,
        "business_date": f"2024-{month}-{day:02d}",
        "currency": currency,
        "amount": amount,
    }
    if voucher_ref is not None:
        row["voucher_ref"] = voucher_ref
    if base_amount is not None:
        row["base_amount"] = base_amount
    if recorded_rate is not None:
        row["recorded_rate"] = recorded_rate
    return row


def make_facts():
    return [
        fact("E-1", "PV-1", 5, "USD", "100.00", "720.00",
             source_ref="SRC-1", recorded_rate="7.2"),
        fact("E-2", "PV-2", 6, "USD", "50.00", "360.50"),
        fact("E-3", "PV-3", 7, "EUR", "10.00", "78.00"),
        fact("E-4", "PV-4", 8, "USD", "30.00", "216.00"),
        # Reversed before the cutoff -> out of scope.
        fact("E-5", "PV-5", 9, "USD", "20.00", "144.00"),
        # Reversed after the cutoff -> still in scope.
        fact("E-6", "PV-6", 10, "USD", "11.00", "79.20"),
        # Dated after the cutoff -> out of scope.
        fact("E-7", "PV-7", 2, "USD", "5.00", "36.00", month="04"),
        fact("E-8", "PV-8", 11, "JPY", "1000", "48.00"),
        fact("E-9", "PV-9", 12, "USD", "100.00", "720.00",
             source_ref="SRC-AMT"),
        fact("E-10", "PV-10", 13, "USD", "100.00", "720.00",
             source_ref="SRC-CUR"),
        fact("E-11", "PV-11", 14, "USD", "100.00", "720.00",
             source_ref="SRC-BASE"),
        # Two identifier-free facts sharing one attribute key.
        fact("E-12", "PV-12", 20, "USD", "40.00", "288.00"),
        fact("E-13", "PV-13", 20, "USD", "40.00", "288.00"),
        # One voucher referenced by one external line but two facts.
        fact("E-14", "PV-MULTI", 21, "USD", "1.00", "7.20", line_no=1),
        fact("E-15", "PV-MULTI", 21, "USD", "2.00", "14.40", line_no=2),
        # Attribute decoy for the dangling voucher reference below.
        fact("E-16", "PV-DECOY", 16, "USD", "5.00", "36.00"),
        # Fact echoing a source id the snapshot does not contain.
        fact("E-17", "PV-G1", 17, "USD", "6.00", "43.20",
             source_ref="GHOST"),
        # Another account: always out of scope for an ACCOUNT report.
        fact("E-18", "PV-OTHER", 12, "USD", "999.00", "7192.80",
             account="6601"),
    ]


def make_reversals():
    return [
        {"entry_id": "E-5", "date": "2024-02-20"},
        {"entry_id": "E-6", "date": "2024-04-05"},
    ]


def make_external():
    return [
        line("SRC-1", 5, "USD", "100.00", voucher_ref="PV-1",
             base_amount="720.00"),
        line("SRC-2", 6, "USD", "50.00", voucher_ref="PV-2",
             base_amount="360.50"),
        line("SRC-3", 7, "EUR", "10.00"),
        line("SRC-AMT", 12, "USD", "99.00", base_amount="712.80"),
        line("SRC-CUR", 13, "EUR", "100.00"),
        line("SRC-BASE", 14, "USD", "100.00", base_amount="719.00"),
        line("SRC-MISS-BOOK", 15, "USD", "9.99"),
        line("SRC-DANGLING", 16, "USD", "5.00",
             voucher_ref="NO-SUCH-PV"),
        line("SRC-DECOY2", 17, "USD", "6.00"),
        line("SRC-REV-AMB", 21, "USD", "1.00", voucher_ref="PV-MULTI"),
        line("SRC-AMB-EXT", 20, "USD", "40.00"),
    ]


def make_book_set(*, periods=None, facts=None, reversals=None,
                  accounts=None, code=BOOK, name="主账套",
                  base_currency=BASE):
    return {
        "code": code,
        "name": name,
        "base_currency": base_currency,
        "accounts": make_accounts() if accounts is None else accounts,
        "periods": make_periods() if periods is None else periods,
        "facts": make_facts() if facts is None else facts,
        "reversals": make_reversals() if reversals is None else reversals,
    }


def make_catalog(**kwargs):
    return [make_book_set(**kwargs)]


def build(external=None, *, cutoff=CUTOFF, account=ACCOUNT,
          catalog=None, book=BOOK):
    if external is None:
        external = make_external()
    if catalog is None:
        catalog = make_catalog()
    return build_reconciliation_report(
        catalog, book, account, cutoff, external
    )


def statuses(items):
    return [item["status"] for item in items]


def summaries_by_currency(report):
    return {row["currency"]: row for row in report["summaries"]}


def all_records(report):
    """Every (kind, id) evidence pair appearing in the report."""
    found = []
    for section in ("matches", "differences"):
        for item in report[section]:
            if item.get("external") is not None:
                found.append(("external", item["external"]["source_id"]))
            if item.get("book") is not None:
                found.append(("book", item["book"]["entry_id"]))
            if item.get("ambiguous_with") is not None:
                for member in item["ambiguous_with"]["records"]:
                    record = member["record"]
                    ident = (record.get("entry_id")
                             if member["side"] == "book"
                             else record["source_id"])
                    found.append((member["side"], ident))
    return found


# --------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------- #
class PublicSurfaceTests(unittest.TestCase):
    def test_exported_names_and_leaf_hierarchy(self):
        import ledger_engine

        for name in ("build_reconciliation_report",
                     "InvalidReconciliationInputError",
                     "TargetNotFoundError",
                     "PeriodStatusConflictError"):
            self.assertIn(name, ledger_engine.__all__)
            self.assertTrue(hasattr(ledger_engine, name))
        for exc in (InvalidReconciliationInputError, TargetNotFoundError,
                    PeriodStatusConflictError):
            self.assertTrue(issubclass(exc, LedgerEngineError))

    def test_report_is_json_serializable(self):
        report = build()
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertEqual(json.loads(encoded), report)


# --------------------------------------------------------------------- #
# Report shape
# --------------------------------------------------------------------- #
class ReportShapeTests(unittest.TestCase):
    def test_top_level_field_order_and_header_values(self):
        report = build()
        self.assertEqual(
            list(report),
            ["book_set_code", "book_set_name", "account_code",
             "account_name", "base_currency", "cutoff", "counts",
             "matches", "differences", "summaries", "base_totals"],
        )
        self.assertEqual(report["book_set_code"], BOOK)
        self.assertEqual(report["book_set_name"], "主账套")
        self.assertEqual(report["account_code"], ACCOUNT)
        self.assertEqual(report["account_name"], "银行存款")
        self.assertEqual(report["base_currency"], BASE)
        self.assertEqual(report["cutoff"], CUTOFF)

    def test_counts_field_order_and_totals(self):
        report = build()
        self.assertEqual(
            list(report["counts"]),
            ["exact_match", "missing_on_books", "missing_externally",
             "original_amount_mismatch", "currency_mismatch",
             "base_conversion_mismatch", "ambiguous"],
        )
        self.assertEqual(
            report["counts"],
            {"exact_match": 3, "missing_on_books": 3,
             "missing_externally": 5, "original_amount_mismatch": 1,
             "currency_mismatch": 1, "base_conversion_mismatch": 1,
             "ambiguous": 2},
        )
        self.assertEqual(report["base_totals"], {
            "currency": "CNY",
            "external_base_total": "2590.30",
            "book_base_total": "4338.50",
            "base_gap": "-1748.20",
        })


# --------------------------------------------------------------------- #
# The seven categories
# --------------------------------------------------------------------- #
class ClassificationTests(unittest.TestCase):
    def test_status_sequence_is_ordered_by_category_then_business_key(self):
        report = build()
        self.assertEqual(
            statuses(report["differences"]),
            ["missing_on_books", "missing_on_books", "missing_on_books",
             "missing_externally", "missing_externally",
             "missing_externally", "missing_externally",
             "missing_externally",
             "original_amount_mismatch", "currency_mismatch",
             "base_conversion_mismatch",
             "ambiguous", "ambiguous"],
        )
        missing_books = [
            item["external"]["source_id"]
            for item in report["differences"]
            if item["status"] == "missing_on_books"
        ]
        self.assertEqual(missing_books,
                         ["SRC-MISS-BOOK", "SRC-DANGLING", "SRC-DECOY2"])
        missing_ext = [
            item["book"]["entry_id"]
            for item in report["differences"]
            if item["status"] == "missing_externally"
        ]
        self.assertEqual(missing_ext,
                         ["E-4", "E-6", "E-8", "E-16", "E-17"])

    def test_exact_matches_are_reference_then_attribute_pairs(self):
        report = build()
        pairs = {
            (item["external"]["source_id"], item["book"]["entry_id"])
            for item in report["matches"]
        }
        self.assertEqual(pairs, {("SRC-1", "E-1"), ("SRC-2", "E-2"),
                                 ("SRC-3", "E-3")})
        self.assertTrue(all(item["status"] == "exact_match"
                            for item in report["matches"]))
        first = report["matches"][0]
        self.assertEqual(
            list(first), ["status", "external", "book", "comparison"])
        self.assertEqual(first["book"]["voucher_id"], "PV-1")
        self.assertEqual(first["book"]["line_no"], 1)
        self.assertEqual(first["external"]["voucher_ref"], "PV-1")
        self.assertEqual(first["book"]["rate"],
                         {"recorded_rate": "7.200000",
                          "effective_rate": "7.200000"})
        self.assertEqual(first["comparison"]["external_base_amount"],
                         "720.00")
        self.assertEqual(first["comparison"]["book_base_amount"], "720.00")

    def test_records_outside_scope_never_appear(self):
        report = build()
        ids = {ident for _kind, ident in all_records(report)}
        # Reversed before cutoff, future fact, other account.
        self.assertNotIn("E-5", ids)
        self.assertNotIn("E-7", ids)
        self.assertNotIn("E-18", ids)
        # The reversal effective after the cutoff does not cancel E-6.
        self.assertIn("E-6", ids)

    def test_original_amount_mismatch_precedes_base_mismatch(self):
        report = build()
        item = next(item for item in report["differences"]
                    if item["status"] == "original_amount_mismatch")
        self.assertEqual(item["external"]["source_id"], "SRC-AMT")
        self.assertEqual(item["book"]["entry_id"], "E-9")
        comparison = item["comparison"]
        self.assertEqual(comparison["external_original_amount"], "99.00")
        self.assertEqual(comparison["book_original_amount"], "100.00")
        # Same currency; base values are still carried as evidence.
        self.assertEqual(comparison["currency_external"], "USD")
        self.assertEqual(comparison["currency_book"], "USD")
        self.assertEqual(comparison["external_base_amount"], "712.80")
        self.assertEqual(comparison["book_base_amount"], "720.00")

    def test_currency_mismatch_splits_the_two_sides(self):
        report = build()
        item = next(item for item in report["differences"]
                    if item["status"] == "currency_mismatch")
        self.assertEqual(item["external"]["source_id"], "SRC-CUR")
        self.assertEqual(item["book"]["entry_id"], "E-10")
        self.assertEqual(item["comparison"]["currency_external"], "EUR")
        self.assertEqual(item["comparison"]["currency_book"], "USD")
        self.assertEqual(item["comparison"]["external_original_amount"],
                         "100.00")
        self.assertEqual(item["comparison"]["book_original_amount"],
                         "100.00")

    def test_base_conversion_mismatch_uses_recorded_amounts_only(self):
        report = build()
        item = next(item for item in report["differences"]
                    if item["status"] == "base_conversion_mismatch")
        self.assertEqual(item["external"]["source_id"], "SRC-BASE")
        self.assertEqual(item["book"]["entry_id"], "E-11")
        self.assertEqual(item["comparison"]["external_base_amount"],
                         "719.00")
        self.assertEqual(item["comparison"]["book_base_amount"], "720.00")
        self.assertEqual(item["comparison"]["book_effective_rate"],
                         "7.200000")

    def test_attribute_ambiguity_groups_all_candidates(self):
        report = build()
        groups = [item for item in report["differences"]
                  if item["status"] == "ambiguous"]
        attribute_group = next(
            item for item in groups
            if item["ambiguous_with"]["reason"]
            == "non_unique_attribute_candidates")
        members = [(m["side"],
                    m["record"].get("entry_id")
                    or m["record"]["source_id"])
                   for m in attribute_group["ambiguous_with"]["records"]]
        self.assertEqual(members,
                         [("book", "E-12"), ("book", "E-13"),
                          ("external", "SRC-AMB-EXT")])
        # Member records carry traceable voucher references and amounts.
        book_members = [m for m in attribute_group["ambiguous_with"]["records"]
                        if m["side"] == "book"]
        self.assertEqual({m["record"]["voucher_id"] for m in book_members},
                         {"PV-12", "PV-13"})
        self.assertTrue(all(m["record"]["original_amount"] == "40.00"
                            for m in attribute_group["ambiguous_with"]["records"]))

    def test_reference_ambiguity_beats_attribute_uniqueness(self):
        # SRC-REV-AMB (USD 1.00 on 02-21) would attribute-match E-14
        # alone, but its voucher_ref pulls both PV-MULTI facts into one
        # disputed component.
        report = build()
        group = next(
            item for item in report["differences"]
            if item["status"] == "ambiguous"
            and item["ambiguous_with"]["reason"]
            == "non_unique_reference_candidates"
            and any(m["record"].get("entry_id") == "E-15"
                    for m in item["ambiguous_with"]["records"]))
        idents = {
            (m["side"], m["record"].get("entry_id")
             or m["record"]["source_id"])
            for m in group["ambiguous_with"]["records"]
        }
        self.assertEqual(idents,
                         {("book", "E-14"), ("book", "E-15"),
                          ("external", "SRC-REV-AMB")})
        # E-14 and E-15 occur nowhere else in the whole report.
        occurrences = [ident for side, ident in all_records(report)
                       if ident in {"E-14", "E-15"}]
        self.assertEqual(sorted(occurrences), ["E-14", "E-15"])

    def test_dangling_identifiers_never_attribute_match(self):
        report = build()
        by_status = {}
        for item in report["differences"]:
            by_status.setdefault(item["status"], []).append(item)
        # SRC-DANGLING points at an unknown voucher: missing on books,
        # despite E-16 sharing currency/amount/date.
        dangling = next(
            item for item in by_status["missing_on_books"]
            if item["external"]["source_id"] == "SRC-DANGLING")
        self.assertIsNone(dangling["book"])
        decoy = next(
            item for item in by_status["missing_externally"]
            if item["book"]["entry_id"] == "E-16")
        self.assertIsNone(decoy["external"])
        # E-17 echoes an absent source id; the ghost-free line is the
        # mirror case on the other side.
        ghost = next(
            item for item in by_status["missing_externally"]
            if item["book"]["entry_id"] == "E-17")
        self.assertIsNone(ghost["external"])
        self.assertIn("SRC-DECOY2",
                      [item["external"]["source_id"]
                       for item in by_status["missing_on_books"]])

    def test_every_record_occurs_exactly_once(self):
        report = build()
        records = all_records(report)
        expected_book = {f"E-{i}" for i in range(1, 18)} - {"E-5", "E-7"}
        expected_external = {
            "SRC-1", "SRC-2", "SRC-3", "SRC-AMT", "SRC-CUR", "SRC-BASE",
            "SRC-MISS-BOOK", "SRC-DANGLING", "SRC-DECOY2", "SRC-REV-AMB",
            "SRC-AMB-EXT",
        }
        book_ids = [i for k, i in records if k == "book"]
        external_ids = [i for k, i in records if k == "external"]
        self.assertEqual(sorted(book_ids), sorted(set(book_ids)))
        self.assertEqual(sorted(external_ids), sorted(set(external_ids)))
        self.assertEqual(set(book_ids), expected_book)
        self.assertEqual(set(external_ids), expected_external)


# --------------------------------------------------------------------- #
# Source-id precedence over voucher reference
# --------------------------------------------------------------------- #
class ReferencePrecedenceTests(unittest.TestCase):
    def test_source_id_echoes_on_each_line_beat_shared_voucher_ref(self):
        # One voucher with two lines; each line echoes its own external
        # source id and both external lines also carry the same
        # voucher_ref.  Source identity must confirm two pairs rather
        # than collapsing the voucher into one ambiguity group.
        facts = [
            fact("L-1", "PV-MULTI", 10, "USD", "100.00", "720.00",
                 line_no=1, source_ref="S-A"),
            fact("L-2", "PV-MULTI", 10, "USD", "200.00", "1440.00",
                 line_no=2, source_ref="S-B"),
        ]
        external = [
            line("S-A", 10, "USD", "100.00", voucher_ref="PV-MULTI"),
            line("S-B", 10, "USD", "200.00", voucher_ref="PV-MULTI"),
        ]
        report = build(external,
                       catalog=make_catalog(facts=facts, reversals=[]))
        self.assertEqual(report["counts"]["exact_match"], 2)
        self.assertEqual(report["counts"]["ambiguous"], 0)
        self.assertEqual(report["differences"], [])
        pairs = {(i["external"]["source_id"], i["book"]["entry_id"])
                 for i in report["matches"]}
        self.assertEqual(pairs, {("S-A", "L-1"), ("S-B", "L-2")})

    def test_one_echoed_sibling_frees_the_other_for_voucher_ref(self):
        # Only one line echoes a source id; the other external line uses
        # only the shared voucher_ref and must pair with the remaining
        # sibling -- source identity of line 1 removes it from the
        # voucher subgraph instead of contaminating line 2.
        facts = [
            fact("L-1", "PV-MULTI", 10, "USD", "100.00", "720.00",
                 line_no=1, source_ref="S-A"),
            fact("L-2", "PV-MULTI", 10, "USD", "200.00", "1440.00",
                 line_no=2),
        ]
        external = [
            line("S-A", 10, "USD", "100.00", voucher_ref="PV-MULTI"),
            line("S-B", 10, "USD", "200.00", voucher_ref="PV-MULTI"),
        ]
        report = build(external,
                       catalog=make_catalog(facts=facts, reversals=[]))
        self.assertEqual(report["counts"]["exact_match"], 2)
        self.assertEqual(report["counts"]["ambiguous"], 0)

    def test_echoed_sibling_and_voucher_only_line_still_ambiguous_when_multi(self):
        # Line 1 is strongly identified; two remaining voucher-only lines
        # and one remaining sibling still form a disputed voucher group.
        facts = [
            fact("L-1", "PV-M", 10, "USD", "1.00", "7.20",
                 line_no=1, source_ref="S-A"),
            fact("L-2", "PV-M", 10, "USD", "2.00", "14.40", line_no=2),
        ]
        external = [
            line("S-A", 10, "USD", "1.00", voucher_ref="PV-M"),
            line("S-B", 10, "USD", "2.00", voucher_ref="PV-M"),
            line("S-C", 10, "USD", "2.00", voucher_ref="PV-M"),
        ]
        report = build(external,
                       catalog=make_catalog(facts=facts, reversals=[]))
        self.assertEqual(report["counts"]["exact_match"], 1)
        self.assertEqual(report["counts"]["ambiguous"], 1)
        group = report["differences"][0]
        idents = {
            (m["side"], m["record"].get("entry_id")
             or m["record"]["source_id"])
            for m in group["ambiguous_with"]["records"]
        }
        self.assertEqual(idents, {("book", "L-2"),
                                  ("external", "S-B"),
                                  ("external", "S-C")})


# --------------------------------------------------------------------- #
# Empty snapshot
# --------------------------------------------------------------------- #
class EmptySnapshotTests(unittest.TestCase):
    def test_empty_snapshot_lists_every_in_scope_fact_as_missing(self):
        report = build([])
        self.assertEqual(report["counts"]["exact_match"], 0)
        self.assertEqual(report["counts"]["missing_on_books"], 0)
        # 18 facts - E-5 (reversed by cutoff) - E-7 (future)
        # - E-18 (other account) = 15.
        self.assertEqual(report["counts"]["missing_externally"], 15)
        self.assertEqual(report["matches"], [])
        self.assertTrue(all(item["status"] == "missing_externally"
                            for item in report["differences"]))
        # Stable business-key order: business date, then entry id.
        self.assertEqual(
            [item["book"]["entry_id"] for item in report["differences"]],
            ["E-1", "E-2", "E-3", "E-4", "E-6", "E-8", "E-9", "E-10",
             "E-11", "E-16", "E-17", "E-12", "E-13", "E-14", "E-15"])

    def test_completely_empty_books_and_snapshot(self):
        catalog = [{
            "code": "EMPTY", "name": "空账套", "base_currency": "CNY",
            "accounts": make_accounts(),
            "periods": make_periods(),
            "facts": [], "reversals": [],
        }]
        report = build([], catalog=catalog, book="EMPTY")
        self.assertEqual(report["matches"], [])
        self.assertEqual(report["differences"], [])
        self.assertEqual(report["counts"]["exact_match"], 0)
        self.assertEqual(report["base_totals"]["base_gap"], "0.00")


# --------------------------------------------------------------------- #
# Scope: cutoff, reversals, accounts
# --------------------------------------------------------------------- #
class ScopeTests(unittest.TestCase):
    def _catalog_with(self, **kwargs):
        facts = kwargs.pop("facts", None)
        reversals = kwargs.pop("reversals", None)
        return make_catalog(facts=facts, reversals=reversals, **kwargs)

    def test_cutoff_excludes_later_facts_but_inclusive_boundary_keeps_equal_day(self):
        facts = [
            fact("B-1", "B-1", 10, "USD", "1.00", "7.20", month="03"),
            fact("B-2", "B-2", 11, "USD", "2.00", "14.40", month="03"),
        ]
        report = build([], cutoff="2024-03-10",
                       catalog=self._catalog_with(facts=facts, reversals=[]))
        ids = [item["book"]["entry_id"] for item in report["differences"]]
        self.assertEqual(ids, ["B-1"])

    def test_reversal_on_cutoff_day_cancels_inclusively(self):
        facts = [fact("B-1", "B-1", 10, "USD", "1.00", "7.20", month="03")]
        reversals = [{"entry_id": "B-1", "date": "2024-03-10"}]
        report = build([], cutoff="2024-03-10",
                       catalog=self._catalog_with(facts=facts,
                                                  reversals=reversals))
        self.assertEqual(report["differences"], [])

    def test_reversal_after_cutoff_does_not_cancel(self):
        facts = [fact("B-1", "B-1", 10, "USD", "1.00", "7.20", month="03")]
        reversals = [{"entry_id": "B-1", "date": "2024-03-11"}]
        report = build([], cutoff="2024-03-10",
                       catalog=self._catalog_with(facts=facts,
                                                  reversals=reversals))
        self.assertEqual(len(report["differences"]), 1)

    def test_other_account_facts_are_excluded(self):
        facts = [
            fact("B-1", "B-1", 10, "USD", "1.00", "7.20", account="1002"),
            fact("B-2", "B-2", 10, "USD", "9.00", "64.80", account="6601"),
        ]
        report = build([], catalog=self._catalog_with(facts=facts,
                                                       reversals=[]))
        ids = [item["book"]["entry_id"] for item in report["differences"]]
        self.assertEqual(ids, ["B-1"])


# --------------------------------------------------------------------- #
# Fixed error boundaries
# --------------------------------------------------------------------- #
class ErrorBoundaryTests(unittest.TestCase):
    def assert_leaf(self, exc_type, **kwargs):
        with self.assertRaises(exc_type) as caught:
            build(**kwargs)
        self.assertIs(type(caught.exception), exc_type)
        return caught.exception

    def test_duplicate_source_id_is_invalid_with_order_stable_message(self):
        external_a = [line("SRC-1", 1, "USD", "1.00"),
                      line("SRC-2", 2, "USD", "2.00"),
                      line("SRC-1", 3, "USD", "3.00")]
        external_b = list(reversed(external_a))
        with self.assertRaises(InvalidReconciliationInputError) as caught:
            build(external_a)
        message = str(caught.exception)
        self.assertIn("SRC-1", message)
        with self.assertRaises(InvalidReconciliationInputError) as caught_b:
            build(external_b)
        self.assertEqual(str(caught_b.exception), message)

    def test_duplicate_id_never_produces_a_partial_report(self):
        # The exception path constructs no report; additionally a good
        # report built from the same catalog exists independently.
        good = build()
        with self.assertRaises(InvalidReconciliationInputError):
            build([line("SRC-1", 1, "USD", "1.00"),
                   line("SRC-1", 2, "USD", "2.00")])
        self.assertEqual(build(), good)

    def test_smallest_duplicated_id_is_named(self):
        exc = self.assert_leaf(
            InvalidReconciliationInputError,
            external=[line("ZZZ", 1, "USD", "1.00"),
                      line("AAA", 2, "USD", "2.00"),
                      line("ZZZ", 3, "USD", "3.00"),
                      line("AAA", 4, "USD", "4.00")])
        self.assertIn("AAA", str(exc))

    def test_malformed_snapshot_shapes_are_invalid_input(self):
        for bad in (None, {}, "x", [{"source_id": "S-1"}]):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(InvalidReconciliationInputError):
                    build_reconciliation_report(
                        make_catalog(), BOOK, ACCOUNT, CUTOFF, bad)

    def test_bad_amount_and_currency_spellings_are_invalid(self):
        self.assert_leaf(
            InvalidReconciliationInputError,
            external=[line("S-1", 1, "USD", "1.005")])
        self.assert_leaf(
            InvalidReconciliationInputError,
            external=[line("S-1", 1, "JPY", "100.5")])
        self.assert_leaf(
            InvalidReconciliationInputError,
            external=[line("S-1", 1, "usd", "1.00")])
        self.assert_leaf(
            InvalidReconciliationInputError,
            external=[line("S-1", 1, "USD", "1.00",
                           base_amount="x")])

    def test_unknown_book_set_is_target_not_found(self):
        self.assert_leaf(TargetNotFoundError, book="NOPE",
                         catalog=make_catalog())

    def test_unknown_account_is_target_not_found(self):
        self.assert_leaf(TargetNotFoundError, account="9999")

    def test_target_boundary_precedes_snapshot_validation(self):
        # Even a duplicate-id snapshot must not mask a missing target.
        bad_snapshot = [line("S-1", 1, "USD", "1.00"),
                        line("S-1", 2, "USD", "2.00")]
        with self.assertRaises(TargetNotFoundError):
            build_reconciliation_report(
                make_catalog(), "NOPE", ACCOUNT, CUTOFF, bad_snapshot)

    def test_bad_cutoff_is_invalid_input(self):
        for bad in (None, 20240331, "2024-13-01", "2024/03/31", "2024-02-30"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidReconciliationInputError):
                    build_reconciliation_report(
                        make_catalog(), BOOK, ACCOUNT, bad, [])

    def test_unclosed_historical_period_conflicts(self):
        periods = [
            {"start": "2024-01-01", "closed": True},
            {"start": "2024-02-01", "closed": False},
            {"start": "2024-03-01", "closed": True},
        ]
        with self.assertRaises(PeriodStatusConflictError) as caught:
            build([], cutoff="2024-02-15",
                  catalog=make_catalog(periods=periods))
        self.assertIn("2024-02-01", str(caught.exception))

    def test_open_current_period_is_allowed(self):
        report = build([], cutoff="2024-04-15")
        self.assertEqual(report["cutoff"], "2024-04-15")

    def test_closed_historical_period_is_allowed(self):
        report = build([], cutoff="2024-02-20")
        self.assertEqual(report["cutoff"], "2024-02-20")

    def test_cutoff_before_first_period_conflicts(self):
        with self.assertRaises(PeriodStatusConflictError):
            build([], cutoff="2023-12-31")

    def test_periods_may_be_supplied_unsorted(self):
        periods = list(reversed(make_periods()))
        report = build([], cutoff="2024-02-15",
                       catalog=make_catalog(periods=periods))
        self.assertEqual(report["cutoff"], "2024-02-15")

    def test_no_period_metadata_means_no_period_constraint(self):
        report = build([], cutoff="2020-01-01",
                       catalog=make_catalog(periods=[]))
        self.assertEqual(report["cutoff"], "2020-01-01")

    def test_malformed_catalog_is_invalid_input(self):
        with self.assertRaises(InvalidReconciliationInputError):
            build_reconciliation_report(
                [{"code": "X"}], BOOK, ACCOUNT, CUTOFF, [])
        with self.assertRaises(InvalidReconciliationInputError):
            build_reconciliation_report(
                "not-a-list", BOOK, ACCOUNT, CUTOFF, [])

    def test_failed_calls_never_mutate_inputs(self):
        catalog = make_catalog()
        external = [line("S-1", 1, "USD", "1.00"),
                    line("S-1", 2, "USD", "2.00")]
        snapshots = copy.deepcopy((catalog, external))
        with self.assertRaises(InvalidReconciliationInputError):
            build_reconciliation_report(
                catalog, BOOK, ACCOUNT, CUTOFF, external)
        self.assertEqual((catalog, external), snapshots)


# --------------------------------------------------------------------- #
# Summaries: per-currency originals, single-currency base grand totals
# --------------------------------------------------------------------- #
class SummaryTests(unittest.TestCase):
    def test_original_amounts_are_never_summed_across_currencies(self):
        report = build()
        rows = summaries_by_currency(report)
        self.assertEqual(set(rows), {"USD", "EUR", "JPY"})
        usd, eur, jpy = rows["USD"], rows["EUR"], rows["JPY"]
        self.assertEqual(usd["matched_original_total"], "150.00")
        self.assertEqual(usd["matched_base_total"], "1080.50")
        self.assertEqual(usd["external_original_total"], "410.99")
        self.assertEqual(usd["book_original_total"], "585.00")
        self.assertEqual(usd["original_gap"], "-174.01")
        self.assertEqual(usd["missing_on_books_external_original"],
                         "20.99")
        self.assertEqual(usd["missing_externally_book_original"], "52.00")
        self.assertEqual(usd["missing_externally_book_base"], "374.40")
        self.assertEqual(usd["amount_mismatch_count"], 2)
        self.assertEqual(usd["currency_mismatch_count"], 1)
        self.assertEqual(usd["ambiguous_count"], 2)
        # The 100.00 EUR side of the currency mismatch never enters the
        # USD original total.
        self.assertNotIn("100.00", usd["original_gap"])
        self.assertEqual(eur["external_original_total"], "110.00")
        self.assertEqual(eur["book_original_total"], "10.00")
        self.assertEqual(eur["original_gap"], "100.00")
        self.assertEqual(eur["currency_mismatch_count"], 1)
        self.assertEqual(jpy["external_original_total"], "0")
        self.assertEqual(jpy["book_original_total"], "1000")
        self.assertEqual(jpy["original_gap"], "-1000")

    def test_base_grand_totals_sum_only_the_single_base_currency(self):
        report = build()
        self.assertEqual(report["base_totals"]["currency"], "CNY")
        for row in report["summaries"]:
            # Original totals stay denominated in their own currency and
            # therefore never feed a cross-currency original grand total.
            self.assertNotIn("original_grand_total", row)
            # Per-currency base gaps reconcile with the displayed totals.
            external = int(row["external_base_total"].replace(".", ""))
            book = int(row["book_base_total"].replace(".", ""))
            gap = external - book
            self.assertEqual(
                int(row["base_gap"].lstrip("-").replace(".", "")),
                abs(gap))
            self.assertEqual(row["base_gap"].startswith("-"), gap < 0)
        grand_external = sum(
            int(row["external_base_total"].replace(".", ""))
            for row in report["summaries"])
        grand_book = sum(
            int(row["book_base_total"].replace(".", ""))
            for row in report["summaries"])
        self.assertEqual(
            report["base_totals"]["external_base_total"],
            f"{grand_external // 100}.{grand_external % 100:02d}")
        self.assertEqual(
            report["base_totals"]["book_base_total"],
            f"{grand_book // 100}.{grand_book % 100:02d}")

    def test_exact_match_without_external_base_claim_has_zero_base_gap(self):
        facts = [fact("B-1", "B-1", 1, "JPY", "1000", "48.00")]
        external = [line("S-1", 1, "JPY", "1000")]
        report = build(external,
                       catalog=make_catalog(facts=facts, reversals=[]))
        jpy = summaries_by_currency(report)["JPY"]
        self.assertEqual(jpy["external_base_total"], "48.00")
        self.assertEqual(jpy["book_base_total"], "48.00")
        self.assertEqual(jpy["base_gap"], "0.00")
        self.assertEqual(report["base_totals"]["base_gap"], "0.00")


# --------------------------------------------------------------------- #
# Money precision and rounding semantics
# --------------------------------------------------------------------- #
class MoneyRulesTests(unittest.TestCase):
    def test_currency_precisions_parse_and_render_exactly(self):
        self.assertEqual(money.parse_minor_units("1000", "JPY", where="t"),
                         1000)
        self.assertEqual(money.format_minor(1000, "JPY"), "1000")
        self.assertEqual(money.parse_minor_units("1.234", "BHD", where="t"),
                         1234)
        self.assertEqual(money.format_minor(1234, "BHD"), "1.234")
        self.assertEqual(money.parse_minor_units("1.23", "USD", where="t"),
                         123)

    def test_base_amounts_keep_fixed_two_places_regardless_of_code(self):
        # The base reuses the voucher engine's integer-cents rule: a
        # JPY-denominated book still stores/renders two-place cents.
        self.assertEqual(money.BASE_PRECISION, 2)
        self.assertEqual(money.parse_base_cents("100", where="t"), 10000)
        self.assertEqual(money.format_base_cents(10000), "100.00")
        self.assertEqual(money.parse_base_cents("1.05", where="t"), 105)
        for bad in ("1.005", "-1", "1.", ".5", "1,000.00"):
            with self.assertRaises(InvalidReconciliationInputError):
                money.parse_base_cents(bad, where="t")

    def test_signed_amounts(self):
        self.assertEqual(money.parse_minor_units("-50.00", "USD", where="t"),
                         -5000)
        self.assertEqual(money.format_minor(-5000, "USD"), "-50.00")

    def test_half_up_rounding_never_depends_on_decimal_context(self):
        self.assertEqual(money.round_half_up(5, 10), 1)
        self.assertEqual(money.round_half_up(-5, 10), -1)
        self.assertEqual(money.round_half_up(15, 10), 2)
        self.assertEqual(money.round_half_up(4, 10), 0)

    def test_recorded_rate_on_external_line_rounds_half_up(self):
        # 1.00 USD * 7.205 CNY/USD = 7.205 CNY = 720.5 cents -> 721.
        lines = parse_external_lines(
            [line("S-1", 1, "USD", "1.00", recorded_rate="7.205")])
        self.assertEqual(external_effective_base(lines[0]), 721)

    def test_external_rate_base_becomes_a_conversion_mismatch(self):
        facts = [fact("B-1", "B-1", 1, "USD", "1.00", "7.20")]
        external = [line("S-1", 1, "USD", "1.00", recorded_rate="7.205")]
        report = build(external,
                       catalog=make_catalog(facts=facts, reversals=[]))
        self.assertEqual(report["counts"]["base_conversion_mismatch"], 1)
        item = report["differences"][0]
        self.assertEqual(item["comparison"]["external_base_amount"],
                         "7.21")
        self.assertEqual(item["comparison"]["book_base_amount"], "7.20")

    def test_no_current_rate_is_ever_substituted(self):
        # Neither side records a rate and no external base claim exists:
        # a same-currency equal-amount pair is an exact match even though
        # the base amount cannot be recomputed from any "latest" rate.
        facts = [fact("B-1", "B-1", 1, "USD", "1.00", "720.00")]
        external = [line("S-1", 1, "USD", "1.00")]
        report = build(external,
                       catalog=make_catalog(facts=facts, reversals=[]))
        self.assertEqual(report["counts"]["exact_match"], 1)
        item = report["matches"][0]
        self.assertIsNone(item["comparison"]["external_base_amount"])
        self.assertIsNone(item["comparison"]["external_rate"])
        self.assertEqual(item["comparison"]["book_base_amount"], "720.00")


# --------------------------------------------------------------------- #
# Determinism: permutations, hash seeds, fresh containers
# --------------------------------------------------------------------- #
class DeterminismTests(unittest.TestCase):
    def test_report_bytes_identical_under_sampled_fact_permutations(self):
        # The rich fixture has too many facts for exhaustive permutation;
        # these deterministic orderings cover reverse/rotate/interleave
        # and several block swaps.
        facts = make_facts()
        reversals = make_reversals()
        n = len(facts)
        orders = [
            list(reversed(range(n))),
            list(range(1, n)) + [0],
            list(range(0, n, 2)) + list(range(1, n, 2)),
            list(range(n // 2, n)) + list(range(n // 2)),
            [n - 1, 0] + list(range(1, n - 1)),
        ]
        reference = json.dumps(build(), **JSON_KW)
        for order in orders:
            self.assertEqual(sorted(order), list(range(n)))
            shuffled = [facts[i] for i in order]
            catalog = make_catalog(facts=copy.deepcopy(shuffled),
                                   reversals=copy.deepcopy(reversals))
            self.assertEqual(json.dumps(build(catalog=catalog), **JSON_KW),
                             reference)

    def test_report_bytes_identical_under_every_small_fixture_permutation(self):
        # Four facts, every 4! ordering must serialize identically.
        facts = [
            fact("P-1", "P-1", 4, "USD", "4.00", "28.80"),
            fact("P-2", "P-2", 1, "EUR", "1.00", "7.80", source_ref="S-2"),
            fact("P-3", "P-3", 3, "USD", "3.00", "21.60"),
            fact("P-4", "P-4", 2, "USD", "2.00", "14.40"),
        ]
        external = [
            line("S-9", 9, "USD", "9.00"),
            line("S-2", 1, "EUR", "1.00", voucher_ref="P-2"),
            line("S-X", 2, "USD", "2.00"),
            line("S-Y", 4, "USD", "4.00"),
        ]
        reference = None
        for fact_order in itertools.permutations(range(len(facts))):
            shuffled_facts = [facts[i] for i in fact_order]
            catalog = make_catalog(facts=copy.deepcopy(shuffled_facts),
                                   reversals=[])
            for line_order in itertools.permutations(
                    range(len(external))):
                shuffled_lines = [external[i] for i in line_order]
                text = json.dumps(build(copy.deepcopy(shuffled_lines),
                                        catalog=catalog), **JSON_KW)
                if reference is None:
                    reference = text
                else:
                    self.assertEqual(text, reference)

    def test_semantically_equal_rebuilt_inputs_serialize_identically(self):
        first = build()
        second = build_reconciliation_report(
            copy.deepcopy(make_catalog()), BOOK, ACCOUNT, CUTOFF,
            copy.deepcopy(make_external()))
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, **JSON_KW),
                         json.dumps(second, **JSON_KW))

    def test_repeated_calls_share_no_mutable_containers(self):
        first = build()
        second = build()
        self.assertIsNot(first, second)

        def mutable_ids(value):
            found = set()

            def walk(node):
                if isinstance(node, (dict, list)):
                    found.add(id(node))
                    children = (node.values() if isinstance(node, dict)
                                else node)
                    for child in children:
                        walk(child)

            walk(value)
            return found

        self.assertTrue(mutable_ids(first).isdisjoint(mutable_ids(second)))

    def test_inputs_are_not_mutated(self):
        catalog = make_catalog()
        external = make_external()
        snapshots = copy.deepcopy((catalog, external))
        build(external, catalog=catalog)
        self.assertEqual((catalog, external), snapshots)

    def test_corrupting_a_result_never_reaches_a_later_call(self):
        first = build()
        first["differences"].clear()
        first["counts"]["ambiguous"] = 99
        second = build()
        self.assertEqual(len(second["differences"]), 13)
        self.assertEqual(second["counts"]["ambiguous"], 2)

    def test_extra_input_fields_are_ignored(self):
        catalog = make_catalog()
        catalog[0]["extra"] = "ignored"
        external = make_external()
        external[0]["marker"] = 123
        report = build(external, catalog=catalog)
        self.assertNotIn("extra", report)
        self.assertNotIn("marker", report["matches"][0]["external"])

    CHILD = (
        "import json, os, sys;"
        "sys.path.insert(0, os.environ['LEDGER_ENGINE_ROOT']);"
        "from ledger_engine import build_reconciliation_report as b;"
        "catalog=[{'code':'BS','name':'账套','base_currency':'CNY',"
        "'accounts':[{'code':'1002','name':'银行','normal_side':'debit',"
        "'active':True}],"
        "'periods':[{'start':'2024-01-01','closed':True}],"
        "'facts':["
        "{'entry_id':'E-3','voucher_id':'V-3','line_no':1,'date':'2024-01-03',"
        "'account_code':'1002','currency':'USD','original_amount':'3.00',"
        "'base_amount':'21.60'},"
        "{'entry_id':'E-1','voucher_id':'V-1','line_no':1,'date':'2024-01-01',"
        "'account_code':'1002','currency':'USD','original_amount':'1.00',"
        "'base_amount':'7.20','source_ref':'S-1'},"
        "{'entry_id':'E-2','voucher_id':'V-2','line_no':1,'date':'2024-01-02',"
        "'account_code':'1002','currency':'EUR','original_amount':'2.00',"
        "'base_amount':'15.60'}],"
        "'reversals':[]}];"
        "external=["
        "{'source_id':'S-9','business_date':'2024-01-09','currency':'USD',"
        "'amount':'9.00'},"
        "{'source_id':'S-1','business_date':'2024-01-01','currency':'USD',"
        "'amount':'1.00','voucher_ref':'V-1','base_amount':'7.20'}];"
        "out=b(catalog,'BS','1002','2024-01-31',external);"
        "sys.stdout.buffer.write(json.dumps(out, sort_keys=True,"
        "ensure_ascii=False,separators=(',',':')).encode('utf-8'))"
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

    def test_output_bytes_identical_across_hash_seeds(self):
        reference = self._run_child(0)
        for seed in (1, 2, 7, 12345, "random"):
            with self.subTest(seed=seed):
                self.assertEqual(self._run_child(seed), reference)


# --------------------------------------------------------------------- #
# Historical stability of closed-period reports
# --------------------------------------------------------------------- #
class HistoricalStabilityTests(unittest.TestCase):
    def test_closed_period_report_survives_later_vouchers_rates_reversals(self):
        before = build(cutoff="2024-02-29")
        later_catalog = make_catalog()
        later_catalog[0]["facts"].append(
            fact("E-NEW", "PV-NEW", 5, "USD", "100.00", "730.00",
                 month="04", recorded_rate="7.3"))
        # A reversal executed later (cannot be back-dated into a closed
        # period) references an old fact but is dated after the cutoff.
        later_catalog[0]["reversals"].append(
            {"entry_id": "E-4", "date": "2024-04-02"})
        after = build(cutoff="2024-02-29", catalog=later_catalog)
        self.assertEqual(json.dumps(after, **JSON_KW),
                         json.dumps(before, **JSON_KW))
        self.assertEqual(after, before)


# --------------------------------------------------------------------- #
# Baseline behavior stays untouched
# --------------------------------------------------------------------- #
class BaselineUnaffectedTests(unittest.TestCase):
    def test_existing_public_surface_is_unchanged(self):
        import ledger_engine

        for name in ("normalize_vouchers", "post_vouchers",
                     "reverse_vouchers", "build_trial_balance"):
            self.assertIn(name, ledger_engine.__all__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
