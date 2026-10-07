"""Frozen snapshots for reconciliation: catalog, ledger facts, externals.

Every raw payload is parsed once into immutable records.  Later stages
(matching, reporting) never touch caller dicts, so mutating the supplied
catalog or snapshot after the call cannot reach a result and repeated
calls over equal payloads return equal facts.

A book set carries its base currency, its account list (the same
code/name/normal_side/active shape as the voucher chart), its accounting
periods with closed flags, the posted ledger facts and the reversals
that cancel facts.  Reconciliation only *reads*: nothing here is ever
written back.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Mapping

from ..vouchers.chart import Account, parse_chart
from ..vouchers.structure import _valid_calendar_date
from .errors import InvalidReconciliationInputError
from .money import (
    parse_base_cents,
    parse_minor_units,
    parse_rate,
    valid_currency_code,
)

__all__ = [
    "Period",
    "LedgerFact",
    "ExternalLine",
    "Reversal",
    "BookSet",
    "Catalog",
    "parse_catalog",
    "parse_external_lines",
    "validate_cutoff",
]

_PERIOD_FIELDS = ("start", "closed")
_FACT_FIELDS = (
    "entry_id",
    "voucher_id",
    "line_no",
    "date",
    "account_code",
    "currency",
    "original_amount",
    "base_amount",
)
_REVERSAL_FIELDS = ("entry_id", "date")
_BOOK_SET_FIELDS = ("code", "name", "base_currency", "accounts")
_EXTERNAL_FIELDS = ("source_id", "business_date", "currency", "amount")

@dataclass(frozen=True)
class Period:
    """One accounting period, identified by its first day."""

    start: str
    closed: bool


@dataclass(frozen=True)
class Reversal:
    """A reversal that cancels one ledger fact, effective on ``date``."""

    entry_id: str
    date: str


@dataclass(frozen=True)
class LedgerFact:
    """One posted, not-yet-cancelled ledger fact in exact minor units.

    ``original_minor`` is in the fact currency's minor units;
    ``base_minor`` is always in the book set base currency (precision 2).
    ``recorded_rate`` is the optional rate explicitly stored with the
    fact; the effective rate is always re-derived from the saved amount
    pair, never from a current market rate.  ``source_ref`` optionally
    records the external source's unique id on the book side, enabling
    direct source-id matching; ``voucher_id`` is the voucher reference.
    """

    entry_id: str
    voucher_id: str
    line_no: int
    date: str
    account_code: str
    currency: str
    original_minor: int
    base_minor: int
    recorded_rate: Fraction | None
    source_ref: str | None

    def business_key(self) -> tuple[str, str]:
        """Stable, environment-independent sort key for one fact."""
        return (self.date, self.entry_id)


@dataclass(frozen=True)
class ExternalLine:
    """One external snapshot line in exact minor units."""

    source_id: str
    business_date: str
    currency: str
    original_minor: int
    voucher_ref: str | None
    base_minor: int | None
    recorded_rate: Fraction | None

    def business_key(self) -> tuple[str, str]:
        """Stable, environment-independent sort key for one line."""
        return (self.business_date, self.source_id)


@dataclass(frozen=True)
class BookSet:
    """Immutable snapshot of one book set."""

    code: str
    name: str
    base_currency: str
    accounts: Mapping[str, Account]
    periods: tuple[Period, ...]
    facts: tuple[LedgerFact, ...]
    reversals: Mapping[str, Reversal]

    def get_account(self, code: str) -> Account | None:
        return self.accounts.get(code)


@dataclass(frozen=True)
class Catalog:
    """Immutable table of book sets keyed by exact code."""

    book_sets: Mapping[str, BookSet]

    def get(self, code: str) -> BookSet | None:
        return self.book_sets.get(code)


def _require_non_empty_str(value: object, where: str, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise InvalidReconciliationInputError(
            f"{where}: {field} must be a non-empty string, got {value!r}"
        )
    return value


def _require_calendar_date(value: object, where: str, field: str) -> str:
    if not isinstance(value, str) or not _valid_calendar_date(value):
        raise InvalidReconciliationInputError(
            f"{where}: {field} must be a valid YYYY-MM-DD calendar date, "
            f"got {value!r}"
        )
    return value


def _parse_periods(raw: object, where: str) -> tuple[Period, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InvalidReconciliationInputError(
            f"{where}: periods must be a list"
        )
    periods: list[Period] = []
    seen_starts: set[str] = set()
    for index, item in enumerate(raw):
        p_where = f"{where} period #{index}"
        if not isinstance(item, dict):
            raise InvalidReconciliationInputError(
                f"{p_where}: period must be an object"
            )
        for field in _PERIOD_FIELDS:
            if field not in item:
                raise InvalidReconciliationInputError(
                    f"{p_where}: missing field {field!r}"
                )
        start = _require_calendar_date(item["start"], p_where, "start")
        closed = item["closed"]
        if not isinstance(closed, bool):
            raise InvalidReconciliationInputError(
                f"{p_where}: closed must be a boolean, got {closed!r}"
            )
        if start in seen_starts:
            raise InvalidReconciliationInputError(
                f"{p_where}: duplicate period start {start!r}"
            )
        seen_starts.add(start)
        periods.append(Period(start=start, closed=closed))
    periods.sort(key=lambda period: period.start)
    return tuple(periods)


def _parse_reversals(
    raw: object,
    where: str,
    fact_ids: set[str],
    facts_by_id: Mapping[str, LedgerFact],
) -> Mapping[str, Reversal]:
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, list):
        raise InvalidReconciliationInputError(
            f"{where}: reversals must be a list"
        )
    reversals: dict[str, Reversal] = {}
    for index, item in enumerate(raw):
        r_where = f"{where} reversal #{index}"
        if not isinstance(item, dict):
            raise InvalidReconciliationInputError(
                f"{r_where}: reversal must be an object"
            )
        for field in _REVERSAL_FIELDS:
            if field not in item:
                raise InvalidReconciliationInputError(
                    f"{r_where}: missing field {field!r}"
                )
        entry_id = _require_non_empty_str(item["entry_id"], r_where,
                                          "entry_id")
        reversal_date = _require_calendar_date(item["date"], r_where, "date")
        if entry_id not in fact_ids:
            raise InvalidReconciliationInputError(
                f"{r_where}: reversal references unknown entry_id "
                f"{entry_id!r}"
            )
        if reversal_date < facts_by_id[entry_id].date:
            raise InvalidReconciliationInputError(
                f"{r_where}: reversal date {reversal_date!r} is earlier than "
                f"the reversed entry date {facts_by_id[entry_id].date!r}"
            )
        if entry_id in reversals:
            raise InvalidReconciliationInputError(
                f"{r_where}: entry_id {entry_id!r} is reversed more than "
                f"once"
            )
        reversals[entry_id] = Reversal(entry_id=entry_id, date=reversal_date)
    return MappingProxyType(reversals)


def _parse_facts(
    raw: object,
    where: str,
    accounts: Mapping[str, Account],
) -> tuple[LedgerFact, ...]:
    if not isinstance(raw, list):
        raise InvalidReconciliationInputError(
            f"{where}: facts must be a list"
        )
    facts: list[LedgerFact] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        f_where = f"{where} fact #{index}"
        if not isinstance(item, dict):
            raise InvalidReconciliationInputError(
                f"{f_where}: fact must be an object"
            )
        for field in _FACT_FIELDS:
            if field not in item:
                raise InvalidReconciliationInputError(
                    f"{f_where}: missing field {field!r}"
                )
        entry_id = _require_non_empty_str(item["entry_id"], f_where,
                                          "entry_id")
        voucher_id = _require_non_empty_str(item["voucher_id"], f_where,
                                            "voucher_id")
        line_no = item["line_no"]
        if isinstance(line_no, bool) or not isinstance(line_no, int) \
                or line_no < 1:
            raise InvalidReconciliationInputError(
                f"{f_where}: line_no must be a positive integer, "
                f"got {line_no!r}"
            )
        fact_date = _require_calendar_date(item["date"], f_where, "date")
        account_code = _require_non_empty_str(item["account_code"], f_where,
                                              "account_code")
        currency = item["currency"]
        if not valid_currency_code(currency):
            raise InvalidReconciliationInputError(
                f"{f_where}: currency must be three uppercase letters, "
                f"got {currency!r}"
            )
        if account_code not in accounts:
            raise InvalidReconciliationInputError(
                f"{f_where}: fact references unknown account code "
                f"{account_code!r}"
            )
        original_minor = parse_minor_units(
            item["original_amount"], currency,
            where=f"{f_where}.original_amount",
        )
        base_minor = parse_base_cents(
            item["base_amount"], where=f"{f_where}.base_amount"
        )
        recorded_rate = None
        if item.get("recorded_rate") is not None:
            recorded_rate = parse_rate(
                item["recorded_rate"], where=f"{f_where}.recorded_rate"
            )
        source_ref = None
        if item.get("source_ref") is not None:
            source_ref = _require_non_empty_str(
                item["source_ref"], f_where, "source_ref"
            )
        if entry_id in seen_ids:
            raise InvalidReconciliationInputError(
                f"{f_where}: duplicate ledger entry_id {entry_id!r}"
            )
        seen_ids.add(entry_id)
        facts.append(
            LedgerFact(
                entry_id=entry_id,
                voucher_id=voucher_id,
                line_no=line_no,
                date=fact_date,
                account_code=account_code,
                currency=currency,
                original_minor=original_minor,
                base_minor=base_minor,
                recorded_rate=recorded_rate,
                source_ref=source_ref,
            )
        )
    return tuple(facts)


def _parse_book_set(item: object, index: int) -> BookSet:
    where = f"book set #{index}"
    if not isinstance(item, dict):
        raise InvalidReconciliationInputError(
            f"{where}: book set must be an object"
        )
    for field in _BOOK_SET_FIELDS:
        if field not in item:
            raise InvalidReconciliationInputError(
                f"{where}: missing field {field!r}"
            )
    code = _require_non_empty_str(item["code"], where, "code")
    name = _require_non_empty_str(item["name"], where, "name")
    # Reuse the exact chart parser: base currency + account snapshot
    # rules stay identical to the rest of the engine.
    chart = parse_chart(item["base_currency"], item["accounts"])
    periods = _parse_periods(item.get("periods"), where)
    facts = _parse_facts(item.get("facts", []), where, chart.accounts)
    facts_by_id = {fact.entry_id: fact for fact in facts}
    reversals = _parse_reversals(
        item.get("reversals"), where, set(facts_by_id), facts_by_id
    )
    return BookSet(
        code=code,
        name=name,
        base_currency=chart.base_currency,
        accounts=chart.accounts,
        periods=periods,
        facts=facts,
        reversals=reversals,
    )


def parse_catalog(book_sets: object) -> Catalog:
    """Validate the raw book-set list into an immutable catalog."""
    if not isinstance(book_sets, list):
        raise InvalidReconciliationInputError("book_sets must be a list")
    parsed: dict[str, BookSet] = {}
    for index, item in enumerate(book_sets):
        book_set = _parse_book_set(item, index)
        if book_set.code in parsed:
            raise InvalidReconciliationInputError(
                f"book set #{index}: duplicate book set code "
                f"{book_set.code!r}"
            )
        parsed[book_set.code] = book_set
    return Catalog(book_sets=MappingProxyType(parsed))


def parse_external_lines(
    external_details: object,
) -> tuple[ExternalLine, ...]:
    """Validate the raw external snapshot into immutable lines.

    An optional ``base_amount`` is always parsed in the engine's fixed
    two-place integer-cents precision, independent of any currency code.

    Structural validation runs first; duplicate source ids are checked
    in a second pass and the *smallest* duplicated id (stable textual
    order) is reported, so the failure never depends on input ordering.
    A duplicate id is a fixed invalid-input boundary: no report can be
    produced from such a snapshot.
    """
    if not isinstance(external_details, list):
        raise InvalidReconciliationInputError(
            "external_details must be a list"
        )
    lines: list[ExternalLine] = []
    for index, item in enumerate(external_details):
        where = f"external detail #{index}"
        if not isinstance(item, dict):
            raise InvalidReconciliationInputError(
                f"{where}: external detail must be an object"
            )
        for field in _EXTERNAL_FIELDS:
            if field not in item:
                raise InvalidReconciliationInputError(
                    f"{where}: missing field {field!r}"
                )
        source_id = _require_non_empty_str(item["source_id"], where,
                                           "source_id")
        business_date = _require_calendar_date(
            item["business_date"], where, "business_date"
        )
        currency = item["currency"]
        if not valid_currency_code(currency):
            raise InvalidReconciliationInputError(
                f"{where}: currency must be three uppercase letters, "
                f"got {currency!r}"
            )
        original_minor = parse_minor_units(
            item["amount"], currency, where=f"{where}.amount"
        )
        voucher_ref = None
        if "voucher_ref" in item and item["voucher_ref"] is not None:
            voucher_ref = _require_non_empty_str(
                item["voucher_ref"], where, "voucher_ref"
            )
        base_minor = None
        if item.get("base_amount") is not None:
            base_minor = parse_base_cents(
                item["base_amount"], where=f"{where}.base_amount"
            )
        recorded_rate = None
        if item.get("recorded_rate") is not None:
            recorded_rate = parse_rate(
                item["recorded_rate"], where=f"{where}.recorded_rate"
            )
        lines.append(
            ExternalLine(
                source_id=source_id,
                business_date=business_date,
                currency=currency,
                original_minor=original_minor,
                voucher_ref=voucher_ref,
                base_minor=base_minor,
                recorded_rate=recorded_rate,
            )
        )
    seen: set[str] = set()
    duplicated: set[str] = set()
    for line in lines:
        if line.source_id in seen:
            duplicated.add(line.source_id)
        seen.add(line.source_id)
    if duplicated:
        duplicate_id = sorted(duplicated)[0]
        raise InvalidReconciliationInputError(
            f"external snapshot contains duplicate source_id "
            f"{duplicate_id!r}"
        )
    return tuple(lines)


def validate_cutoff(cutoff: object) -> str:
    """Validate the cutoff as a real calendar date; return it as-is."""
    return _require_calendar_date(cutoff, "cutoff", "cutoff")
