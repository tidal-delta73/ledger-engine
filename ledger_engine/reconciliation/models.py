"""Immutable records and parsing for ledger facts and external details.

Both sides of a reconciliation are snapshotted into frozen records at the
boundary, so later mutation of the caller's book or snapshot cannot change
a report and every comparison reads the same validated values.

A *ledger fact* is one already-posted ledger line scoped to one account
with its original-currency amount and, when the book records one, its
saved base-currency amount and the rate that produced it.  Facts may be
cancelled by an offset (a reversal pair): an offset only counts *within
the cutoff* when the counterpart fact is itself effective by then.

An *external detail* is one statement row: source-unique id, business
date, currency, signed amount and an optional voucher reference.

Amounts are signed integer cents (the external grammar permits one leading
sign; see :mod:`ledger_engine.reconciliation.amounts`); no float exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

from ..vouchers.structure import _valid_calendar_date
from .amounts import parse_signed_cents
from .errors import ValidationAborted

__all__ = [
    "RateInfo",
    "Offset",
    "LedgerFact",
    "ExternalDetail",
    "parse_book_facts",
    "parse_external_details",
]

_RATE_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


@dataclass(frozen=True)
class RateInfo:
    """The rate exactly as recorded on the ledger fact (never recomputed)."""

    rate: str
    rate_date: str | None
    rate_source: str | None


@dataclass(frozen=True)
class Offset:
    """A reversal pair: the counterpart fact id and the signed amount."""

    fact_id: str
    amount_cents: int


@dataclass(frozen=True)
class LedgerFact:
    """One effective candidate fact after structural validation."""

    fact_id: str
    voucher_id: str | None
    line_no: int | None
    account_code: str
    business_date: str
    effective_date: str
    currency: str
    amount_cents: int
    base_amount_cents: int | None
    rate: RateInfo | None
    voucher_ref: str | None
    reversed_by: tuple[str, ...]
    offsets: tuple[Offset, ...]


@dataclass(frozen=True)
class ExternalDetail:
    """One external statement row after structural validation."""

    source_id: str
    business_date: str
    currency: str
    amount_cents: int
    voucher_ref: str | None


def _require_object(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationAborted(f"{where}: must be an object")
    return value


def _require_fields(obj: dict, fields: tuple[str, ...], where: str) -> None:
    for name in fields:
        if name not in obj:
            raise ValidationAborted(f"{where}: missing field {name!r}")


def _optional_text(value: object, field: str, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ValidationAborted(
            f"{where}: {field} must be a non-empty string or null, "
            f"got {value!r}")
    return value


def _parse_rate(value: object, where: str) -> RateInfo | None:
    if value is None:
        return None
    obj = _require_object(value, f"{where}.rate")
    _require_fields(obj, ("rate",), f"{where}.rate")
    rate = obj["rate"]
    if not isinstance(rate, str) or _RATE_RE.fullmatch(rate) is None:
        raise ValidationAborted(
            f"{where}.rate: rate must be a non-negative fixed-point "
            f"decimal string, got {rate!r}")
    rate_date = obj.get("rate_date")
    if rate_date is not None:
        if not isinstance(rate_date, str) or not _valid_calendar_date(
                rate_date):
            raise ValidationAborted(
                f"{where}.rate: rate_date must be a valid YYYY-MM-DD date "
                f"or null, got {rate_date!r}")
    rate_source = obj.get("rate_source")
    if rate_source is not None and not isinstance(rate_source, str):
        raise ValidationAborted(
            f"{where}.rate: rate_source must be a string or null, "
            f"got {rate_source!r}")
    return RateInfo(rate=rate, rate_date=rate_date,
                    rate_source=rate_source if rate_source else None)


def _parse_offsets(value: object, where: str) -> tuple[Offset, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValidationAborted(f"{where}: offsets must be a list")
    offsets: list[Offset] = []
    for index, item in enumerate(value):
        item_where = f"{where}.offsets[{index}]"
        obj = _require_object(item, item_where)
        _require_fields(obj, ("fact_id", "amount"), item_where)
        fact_id = obj["fact_id"]
        if not isinstance(fact_id, str) or fact_id == "":
            raise ValidationAborted(
                f"{item_where}: fact_id must be a non-empty string, "
                f"got {fact_id!r}")
        amount_cents = parse_signed_cents(
            obj["amount"], where=item_where)
        offsets.append(Offset(fact_id=fact_id, amount_cents=amount_cents))
    return tuple(offsets)


def _parse_reversed_by(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValidationAborted(f"{where}: reversed_by must be a list")
    result: list[str] = []
    for index, item in enumerate(value):
        item_where = f"{where}.reversed_by[{index}]"
        if not isinstance(item, str) or item == "":
            raise ValidationAborted(
                f"{item_where}: reverser id must be a non-empty string, "
                f"got {item!r}")
        result.append(item)
    return tuple(result)


def parse_book_facts(book: object) -> tuple[LedgerFact, ...]:
    """Validate the book payload structurally and snapshot every fact.

    Existence of account codes against the chart is checked by the caller
    once the chart is parsed.  Uniqueness of ``fact_id`` is enforced here.
    """
    obj = _require_object(book, "book")
    facts_value = obj.get("facts")
    if not isinstance(facts_value, list):
        raise ValidationAborted("book.facts must be a list")
    facts: list[LedgerFact] = []
    seen: set[str] = set()
    for index, raw in enumerate(facts_value):
        where = f"book.facts[{index}]"
        item = _require_object(raw, where)
        _require_fields(
            item,
            ("fact_id", "account_code", "date", "currency", "amount"),
            where,
        )
        fact_id = item["fact_id"]
        if not isinstance(fact_id, str) or fact_id == "":
            raise ValidationAborted(
                f"{where}: fact_id must be a non-empty string, "
                f"got {fact_id!r}")
        if fact_id in seen:
            raise ValidationAborted(
                f"{where}: duplicate ledger fact_id {fact_id!r}")
        seen.add(fact_id)

        account_code = item["account_code"]
        if not isinstance(account_code, str) or account_code == "":
            raise ValidationAborted(
                f"{where}: account_code must be a non-empty string, "
                f"got {account_code!r}")
        business_date = item["date"]
        if not isinstance(business_date, str) or not _valid_calendar_date(
                business_date):
            raise ValidationAborted(
                f"{where} ({fact_id}): date must be a valid YYYY-MM-DD "
                f"date, got {business_date!r}")
        currency = item["currency"]
        if not isinstance(currency, str) or currency == "":
            raise ValidationAborted(
                f"{where} ({fact_id}): currency must be a non-empty "
                f"string, got {currency!r}")
        amount_cents = parse_signed_cents(item["amount"], where=where)

        effective_text = item.get("effective_date")
        if effective_text is None:
            effective_text = business_date
        elif not isinstance(effective_text, str) or not _valid_calendar_date(
                effective_text):
            raise ValidationAborted(
                f"{where} ({fact_id}): effective_date must be a valid "
                f"YYYY-MM-DD date or null, got {effective_text!r}")

        base_value = item.get("base_amount")
        base_amount_cents: int | None
        if base_value is None:
            base_amount_cents = None
        else:
            base_amount_cents = parse_signed_cents(
                base_value, where=f"{where}.base_amount")

        voucher_ref = _optional_text(
            item.get("voucher_ref"), "voucher_ref", where)
        voucher_id = _optional_text(
            item.get("voucher_id"), "voucher_id", where)
        line_no = item.get("line_no")
        if line_no is not None and (
                not isinstance(line_no, int) or isinstance(line_no, bool)
                or line_no < 1):
            raise ValidationAborted(
                f"{where} ({fact_id}): line_no must be a positive integer "
                f"or null, got {line_no!r}")

        facts.append(
            LedgerFact(
                fact_id=fact_id,
                voucher_id=voucher_id,
                line_no=line_no,
                account_code=account_code,
                business_date=business_date,
                effective_date=effective_text,
                currency=currency,
                amount_cents=amount_cents,
                base_amount_cents=base_amount_cents,
                rate=_parse_rate(item.get("rate"), where),
                voucher_ref=voucher_ref,
                reversed_by=_parse_reversed_by(
                    item.get("reversed_by"), where),
                offsets=_parse_offsets(item.get("offsets"), where),
            )
        )
    return tuple(facts)


def parse_external_details(snapshot: object) -> tuple[ExternalDetail, ...]:
    """Validate the external snapshot structurally and snapshot its rows.

    Duplicate ``source_id`` is a request-level rule checked separately, so
    this parser only enforces shape and value types.
    """
    if not isinstance(snapshot, list):
        raise ValidationAborted("external_details must be a list")
    details: list[ExternalDetail] = []
    for index, raw in enumerate(snapshot):
        where = f"external_details[{index}]"
        item = _require_object(raw, where)
        _require_fields(
            item, ("source_id", "date", "currency", "amount"), where)
        source_id = item["source_id"]
        if not isinstance(source_id, str) or source_id == "":
            raise ValidationAborted(
                f"{where}: source_id must be a non-empty string, "
                f"got {source_id!r}")
        business_date = item["date"]
        if not isinstance(business_date, str) or not _valid_calendar_date(
                business_date):
            raise ValidationAborted(
                f"{where} ({source_id}): date must be a valid YYYY-MM-DD "
                f"date, got {business_date!r}")
        currency = item["currency"]
        if not isinstance(currency, str) or currency == "":
            raise ValidationAborted(
                f"{where} ({source_id}): currency must be a non-empty "
                f"string, got {currency!r}")
        amount_cents = parse_signed_cents(item["amount"], where=where)
        voucher_ref = _optional_text(
            item.get("voucher_ref"), "voucher_ref", where)
        details.append(
            ExternalDetail(
                source_id=source_id,
                business_date=business_date,
                currency=currency,
                amount_cents=amount_cents,
                voucher_ref=voucher_ref,
            )
        )
    return tuple(details)


def effective_day(fact: LedgerFact) -> date:
    return date.fromisoformat(fact.effective_date)
