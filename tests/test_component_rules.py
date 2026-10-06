"""Component-boundary regression tests for the refactored pipeline.

The public contract lives in ``test_normalize_vouchers.py``; this file
pins the *internal* seams the refactor introduced so that later posting,
reversal and recomputation features can safely reuse each stage:

* amounts  -- the only integer-cents rules;
* chart    -- immutable chart/base-currency snapshot, exact lookup;
* structure-- shape checks never touch accounts or amounts;
* entries  -- per-line business precedence and integer totals;
* assemble -- fixed key insertion order and two-place rendering;
* pipeline -- cross-hash-seed byte determinism and input invariance.

Standard library only; CPython 3.10+.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import subprocess
import sys
import unittest

from ledger_engine import normalize_vouchers
from ledger_engine.vouchers import amounts, assemble, chart as chart_mod
from ledger_engine.vouchers import entries, pipeline, structure
from ledger_engine.vouchers.errors import (
    ChartOfAccountsError,
    InactiveAccountError,
    InvalidEntryAmountError,
    UnknownAccountError,
    VoucherFormatError,
)

BASE = "CNY"
JSON_KW = {"sort_keys": True, "ensure_ascii": False,
           "separators": (",", ":")}


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
    return {"account_code": account_code, "summary": summary,
            "debit": debit, "credit": credit}


def make_voucher(voucher_id="V-1", date_text="2024-01-01", currency=BASE,
                 entries=None):
    if entries is None:
        entries = [
            make_entry("6601", "借", debit="100", credit="00"),
            make_entry("2202", "贷", debit="0.0", credit="100.00"),
        ]
    return {"voucher_id": voucher_id, "date": date_text,
            "currency": currency, "entries": entries}


# --------------------------------------------------------------------- #
# amounts: the single integer-cents rule set
# --------------------------------------------------------------------- #
class AmountRulesTests(unittest.TestCase):
    def test_accepted_forms_parse_to_exact_cents(self):
        cases = {
            "0": 0, "00": 0, "0.0": 0, "0.00": 0,
            "1": 100, "1.0": 100, "1.00": 100,
            "0.1": 10, "0.01": 1, "100.25": 10025,
            "1234.5": 123450, "99999999999999999999.99": 9999999999999999999999,
        }
        for text, cents in cases.items():
            with self.subTest(text=text):
                self.assertEqual(amounts.parse_cents(text, where="t"), cents)

    def test_every_rejected_spelling_raises_leaf_error(self):
        for value in ("-1", "+1", "1e3", "1,000.00", " 100", "100 ", ".5",
                      "1.", "1.234", "0.001", "", "  ", "nan", "NaN", "Inf",
                      "0x10", "1_000", "1.2.3", "１", "①",
                      0, 1, 1.5, True, False, None, [1], {"v": 1}):
            with self.subTest(value=value):
                with self.assertRaises(InvalidEntryAmountError):
                    amounts.parse_cents(value, where="line 7")
                self.assertIs(type(
                    self._catch(value)), InvalidEntryAmountError)

    def _catch(self, value):
        try:
            amounts.parse_cents(value, where="line 7")
        except InvalidEntryAmountError as exc:
            return exc
        self.fail("expected InvalidEntryAmountError")

    def test_error_message_names_location_and_value(self):
        exc = self._catch("1,000.00")
        self.assertIn("line 7", str(exc))
        self.assertIn("'1,000.00'", str(exc))

    def test_format_renders_exactly_two_places(self):
        self.assertEqual(amounts.format_cents(0), "0.00")
        self.assertEqual(amounts.format_cents(5), "0.05")
        self.assertEqual(amounts.format_cents(10025), "100.25")
        self.assertEqual(amounts.format_cents(10000000000000000000000),
                         "100000000000000000000.00")

    def test_parse_format_round_trip_is_identity(self):
        for cents in (0, 1, 9, 10, 99, 100, 101, 123456,
                      10 ** 24, 10 ** 24 + 7):
            self.assertEqual(
                amounts.parse_cents(amounts.format_cents(cents), where="t"),
                cents,
            )


# --------------------------------------------------------------------- #
# chart: immutable snapshot with exact code matching
# --------------------------------------------------------------------- #
class ChartRulesTests(unittest.TestCase):
    def test_base_currency_is_checked_before_chart(self):
        with self.assertRaises(ChartOfAccountsError):
            chart_mod.parse_chart("bad", ["broken-account"])
        with self.assertRaises(ChartOfAccountsError):
            chart_mod.parse_chart("bad", None)

    def test_validated_chart_is_a_frozen_snapshot(self):
        raw = make_chart()
        snapshot = chart_mod.parse_chart(BASE, raw)
        self.assertEqual(snapshot.base_currency, "CNY")
        self.assertIsInstance(snapshot, chart_mod.Chart)
        self.assertIsNone(snapshot.get("8888"))
        self.assertEqual(snapshot.get("1001").name, "库存现金")
        self.assertEqual(snapshot.get("1001").normal_side, "debit")
        self.assertIs(snapshot.get("1001").active, True)

    def test_lookup_is_exact_without_trim_or_case_fold(self):
        snapshot = chart_mod.parse_chart(BASE, make_chart())
        self.assertIsNone(snapshot.get("1001 "))
        self.assertIsNone(snapshot.get(" 1001"))
        self.assertIsNone(snapshot.get("1001\n"))
        self.assertIsNone(snapshot.get(""))

    def test_snapshot_is_immune_to_later_raw_mutation(self):
        raw = make_chart()
        snapshot = chart_mod.parse_chart(BASE, raw)
        raw[0]["name"] = "被篡改的名称"
        raw[0]["active"] = False
        raw.append({"code": "9999", "name": "事后新增",
                    "normal_side": "credit", "active": True})
        self.assertEqual(snapshot.get("1001").name, "库存现金")
        self.assertIs(snapshot.get("1001").active, True)
        self.assertIsNone(snapshot.get("9999"))

    def test_snapshot_objects_and_mapping_cannot_be_mutated(self):
        snapshot = chart_mod.parse_chart(BASE, make_chart())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.get("1001").active = False
        with self.assertRaises(TypeError):
            snapshot.accounts["9999"] = snapshot.get("1001")
        with self.assertRaises(TypeError):
            del snapshot.accounts["1001"]
        with self.assertRaises(AttributeError):
            snapshot.accounts.clear()

    def test_invalid_charts_raise_chart_error_without_mutation(self):
        raw = make_chart()
        snapshot_input = copy.deepcopy(raw)
        with self.assertRaises(ChartOfAccountsError):
            chart_mod.parse_chart(BASE, raw + ["not-an-object"])
        self.assertEqual(raw, snapshot_input)


# --------------------------------------------------------------------- #
# structure: shape-only boundary; accounts and amounts are irrelevant
# --------------------------------------------------------------------- #
class StructureRulesTests(unittest.TestCase):
    def test_shape_valid_voucher_structures_even_with_bad_business_data(self):
        # Unknown account code and garbage amounts must pass the structural
        # stage untouched: those boundaries belong to later stages.
        raw = make_voucher(entries=[
            make_entry("8888", "未知", debit="not-money", credit="also"),
            make_entry("4001", "停用", debit="1.00", credit="0.00"),
        ])
        record = structure.structure_voucher(raw, 3)
        self.assertIsInstance(record, structure.StructuredVoucher)
        self.assertEqual(record.voucher_id, "V-1")
        self.assertEqual(len(record.entries), 2)
        self.assertEqual(record.entries[0].account_code, "8888")
        # Amount text is carried verbatim; it is never parsed here.
        self.assertEqual(record.entries[0].debit_text, "not-money")
        self.assertEqual(record.entries[0].credit_text, "also")
        self.assertIsInstance(record.entries, tuple)

    def test_entries_tuple_is_independent_of_raw_list(self):
        raw = make_voucher()
        record = structure.structure_voucher(raw, 0)
        raw["entries"].append(make_entry("1001", "后来追加"))
        self.assertEqual(len(record.entries), 2)

    def test_every_structural_defect_is_a_format_error(self):
        bad_batches = [
            "not-object",
            {k: v for k, v in make_voucher().items() if k != "date"},
            make_voucher(voucher_id=123),
            make_voucher(date_text="2023-02-29"),
            make_voucher(currency=5),
            make_voucher(entries=[make_entry()]),
            make_voucher(entries=["raw-entry", make_entry()]),
            make_voucher(entries=[
                {k: v for k, v in make_entry().items() if k != "credit"},
                make_entry("2202", "贷", credit="1.00"),
            ]),
            make_voucher(entries=[
                make_entry(1001, "数字代码", debit="1.00"),
                make_entry("2202", "贷", credit="1.00"),
            ]),
            make_voucher(entries=[
                make_entry("6601", 9, debit="1.00"),
                make_entry("2202", "贷", credit="1.00"),
            ]),
        ]
        for index, raw in enumerate(bad_batches):
            with self.subTest(case=index):
                with self.assertRaises(VoucherFormatError):
                    structure.structure_voucher(raw, 0)

    def test_structure_does_not_mutate_input(self):
        raw = make_voucher()
        snapshot = copy.deepcopy(raw)
        structure.structure_voucher(raw, 0)
        self.assertEqual(raw, snapshot)

    def test_batch_must_be_list_boundary(self):
        for payload in (None, {}, tuple(), "x"):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(VoucherFormatError):
                    structure.require_voucher_list(payload)
        self.assertEqual(structure.require_voucher_list([]), [])


# --------------------------------------------------------------------- #
# entries: exact per-line precedence, integer totals
# --------------------------------------------------------------------- #
class EntryRulesTests(unittest.TestCase):
    def setUp(self):
        self.chart = chart_mod.parse_chart(BASE, make_chart())

    def structured(self, entries):
        return structure.structure_voucher(
            make_voucher(entries=entries), 0)

    def test_unknown_account_beats_inactive_and_amount_defects(self):
        record = self.structured([
            make_entry("8888", "坏", debit="bad", credit="also"),
            make_entry("4001", "停用", credit="1.00"),
        ])
        with self.assertRaises(UnknownAccountError):
            entries.validate_entries(record, self.chart, 0)

    def test_inactive_account_beats_amount_defects(self):
        record = self.structured([
            make_entry("4001", "停用且金额坏", debit="bad"),
            make_entry("2202", "贷", credit="1.00"),
        ])
        with self.assertRaises(InactiveAccountError):
            entries.validate_entries(record, self.chart, 0)

    def test_debit_side_amount_checked_before_credit_side(self):
        # Both sides unparseable: debit must be the reported value.
        record = self.structured([
            make_entry("6601", "双边坏", debit="bad-d", credit="bad-c"),
            make_entry("2202", "贷", credit="1.00"),
        ])
        try:
            entries.validate_entries(record, self.chart, 0)
        except InvalidEntryAmountError as exc:
            self.assertIn("'bad-d'", str(exc))
            self.assertNotIn("bad-c", str(exc))
        else:
            self.fail("expected InvalidEntryAmountError")

    def test_good_debit_bad_credit_reports_credit(self):
        record = self.structured([
            make_entry("6601", "贷方坏", debit="1.00", credit="nope"),
            make_entry("2202", "贷", credit="1.00"),
        ])
        try:
            entries.validate_entries(record, self.chart, 0)
        except InvalidEntryAmountError as exc:
            self.assertIn("'nope'", str(exc))
        else:
            self.fail("expected InvalidEntryAmountError")

    def test_exactly_one_side_rule_comes_after_parsing(self):
        for debit, credit in (("0", "0"), ("1.00", "1.00")):
            with self.subTest(debit=debit, credit=credit):
                record = self.structured([
                    make_entry("6601", "x", debit=debit, credit=credit),
                    make_entry("2202", "贷", debit="1.00", credit="0.00"),
                ])
                try:
                    entries.validate_entries(record, self.chart, 0)
                except InvalidEntryAmountError as exc:
                    self.assertIn("exactly one", str(exc))
                else:
                    self.fail("expected InvalidEntryAmountError")

    def test_line_two_defect_reports_line_two(self):
        record = self.structured([
            make_entry("6601", "好", debit="1.00", credit="0.00"),
            make_entry("7777", "第二行未知", credit="1.00"),
        ])
        try:
            entries.validate_entries(record, self.chart, 0)
        except UnknownAccountError as exc:
            self.assertIn("line 2", str(exc))
        else:
            self.fail("expected UnknownAccountError")

    def test_valid_body_holds_integer_cents_and_chart_snapshot(self):
        record = self.structured([
            make_entry("6601", "费用", debit="1234.5", credit="0.00"),
            make_entry("2202", "应付", debit="0", credit="1200.00"),
            make_entry("6601", "调整", debit="0.00", credit="34.5"),
        ])
        body = entries.validate_entries(record, self.chart, 0)
        self.assertEqual(body.debit_total, 123450)
        self.assertEqual(body.credit_total, 123450)
        self.assertEqual([e.line_no for e in body.entries], [1, 2, 3])
        first = body.entries[0]
        self.assertEqual((first.debit_cents, first.credit_cents), (123450, 0))
        self.assertEqual(first.account_name, "管理费用")
        self.assertEqual(first.normal_side, "debit")
        self.assertIsInstance(body.entries, tuple)

    def test_totals_use_exact_integer_arithmetic(self):
        record = self.structured([
            make_entry("6601", "a", debit="0.10", credit="0.00"),
            make_entry("6601", "b", debit="0.20", credit="0.00"),
            make_entry("2202", "c", debit="0.00", credit="0.30"),
        ])
        body = entries.validate_entries(record, self.chart, 0)
        self.assertEqual(body.debit_total, 30)
        self.assertEqual(body.credit_total, 30)


# --------------------------------------------------------------------- #
# assemble: deterministic output construction, fixed key order
# --------------------------------------------------------------------- #
class AssembleRulesTests(unittest.TestCase):
    ENTRY_KEYS = ["line_no", "account_code", "account_name", "normal_side",
                  "summary", "debit", "credit"]
    VOUCHER_KEYS = ["voucher_id", "date", "currency", "entries",
                    "debit_total", "credit_total"]

    def _valid_body(self):
        ch = chart_mod.parse_chart(BASE, make_chart())
        record = structure.structure_voucher(make_voucher(), 0)
        return record, entries.validate_entries(record, ch, 0)

    def test_entry_key_insertion_order_is_contractual(self):
        _, body = self._valid_body()
        doc = assemble.build_entry(body.entries[0])
        self.assertEqual(list(doc.keys()), self.ENTRY_KEYS)

    def test_voucher_key_insertion_order_is_contractual(self):
        record, body = self._valid_body()
        doc = assemble.build_voucher(record, body)
        self.assertEqual(list(doc.keys()), self.VOUCHER_KEYS)
        self.assertEqual([list(e.keys()) for e in doc["entries"]],
                         [self.ENTRY_KEYS, self.ENTRY_KEYS])

    def test_amounts_render_two_places_and_totals_present(self):
        record, body = self._valid_body()
        doc = assemble.build_voucher(record, body)
        self.assertEqual(doc["entries"][0]["debit"], "100.00")
        self.assertEqual(doc["entries"][0]["credit"], "0.00")
        self.assertEqual(doc["debit_total"], "100.00")
        self.assertEqual(doc["credit_total"], "100.00")

    def test_assembled_containers_are_fresh_each_call(self):
        record, body = self._valid_body()
        one = assemble.build_voucher(record, body)
        two = assemble.build_voucher(record, body)
        self.assertEqual(one, two)
        self.assertIsNot(one, two)
        self.assertIsNot(one["entries"], two["entries"])
        self.assertIsNot(one["entries"][0], two["entries"][0])


# --------------------------------------------------------------------- #
# Reuse: a second consumer (recomputation) shares the exact same rules
# --------------------------------------------------------------------- #
class SharedRulesReuseTests(unittest.TestCase):
    def test_recomputed_totals_match_pipeline_via_shared_primitives(self):
        raw_batch = [make_voucher()]
        result = normalize_vouchers(BASE, make_chart(),
                                   copy.deepcopy(raw_batch))

        # A "recompute" path reuses structure + entries + assemble instead
        # of copying any amount/account logic; it must reproduce the
        # pipeline output document exactly.
        ch = chart_mod.parse_chart(BASE, make_chart())
        record = structure.structure_voucher(raw_batch[0], 0)
        body = entries.validate_entries(record, ch, 0)
        recomputed = assemble.build_voucher(record, body)
        self.assertEqual(recomputed, result[0])
        self.assertEqual(json.dumps(recomputed, **JSON_KW),
                         json.dumps(result[0], **JSON_KW))

    def test_balance_judgement_reuses_integer_totals(self):
        # Future posting must not re-derive balance with floats: summing
        # parsed cents through the shared primitive decides equality.
        ch = chart_mod.parse_chart(BASE, make_chart())
        unbalanced = make_voucher(entries=[
            make_entry("6601", "借", debit="0.10", credit="0.00"),
            make_entry("6601", "借", debit="0.20", credit="0.00"),
            make_entry("2202", "短一分", debit="0.00", credit="0.29"),
        ])
        body = entries.validate_entries(
            structure.structure_voucher(unbalanced, 0), ch, 0)
        self.assertNotEqual(body.debit_total, body.credit_total)
        self.assertEqual(abs(body.debit_total - body.credit_total), 1)


# --------------------------------------------------------------------- #
# Pipeline determinism: no time/locale/hash-order/global-state reliance
# --------------------------------------------------------------------- #
class DeterminismTests(unittest.TestCase):
    CHILD = (
        "import json, os, sys;"
        "sys.path.insert(0, os.environ['LEDGER_ENGINE_ROOT']);"
        "from ledger_engine import normalize_vouchers as n;"
        "chart=[{'code':'1001','name':'库存现金','normal_side':'debit',"
        "'active':True},{'code':'2202','name':'应付账款',"
        "'normal_side':'credit','active':True},"
        "{'code':'6601','name':'管理费用','normal_side':'debit',"
        "'active':True},{'code':'4001','name':'停用收入',"
        "'normal_side':'credit','active':False}];"
        "v=[{'voucher_id':'V-1','date':'2024-02-29','currency':'CNY',"
        "'entries':[{'account_code':'6601','summary':'购办公用品',"
        "'debit':'100','credit':'00'},"
        "{'account_code':'2202','summary':'赊购入账','debit':'0.0',"
        "'credit':'100.00'}]},"
        "{'voucher_id':'V-2','date':'2023-03-01','currency':'CNY',"
        "'entries':[{'account_code':'1001','summary':'支付欠款',"
        "'debit':'1234.5','credit':'0.00'},"
        "{'account_code':'2202','summary':'冲减应付','debit':'0',"
        "'credit':'1200.00'},"
        "{'account_code':'6601','summary':'费用调整','debit':'0.00',"
        "'credit':'34.5'}]}];"
        "out=n('CNY',chart,v);"
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

    def test_no_global_mutable_state_between_calls(self):
        first = normalize_vouchers(BASE, make_chart(), [make_voucher()])
        # Poking at module-level state must not change a later result;
        # the stages carry everything through arguments and frozen records.
        self.assertFalse(hasattr(pipeline, "_results"))
        self.assertFalse(hasattr(pipeline, "_seen_ids"))
        second = normalize_vouchers(BASE, make_chart(), [make_voucher()])
        self.assertEqual(first, second)
        self.assertIsNot(first, second)

    def test_inputs_untouched_on_failure_and_stages_leave_no_trace(self):
        raw = make_voucher(entries=[
            make_entry("8888", "未知", debit="bad"),
            make_entry("2202", "贷", credit="1.00"),
        ])
        snapshot = copy.deepcopy(raw)
        with self.assertRaises(UnknownAccountError):
            normalize_vouchers(BASE, make_chart(), [raw])
        self.assertEqual(raw, snapshot)

    def test_extra_fields_everywhere_are_ignored_and_absent_from_output(self):
        chart = make_chart()
        chart[0]["unexpected"] = ["ignored"]
        voucher = make_voucher()
        voucher["marker"] = {"nested": 1}
        voucher["entries"][0]["marker"] = 42
        result = normalize_vouchers(BASE, chart, [voucher])
        self.assertNotIn("marker", result[0])
        self.assertNotIn("marker", result[0]["entries"][0])
        self.assertNotIn("unexpected", result[0]["entries"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
