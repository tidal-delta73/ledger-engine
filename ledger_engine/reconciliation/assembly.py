"""Deterministic assembly of reconciliation report documents.

Only shape assembly lives here -- field names, fixed key insertion order,
two-decimal signed rendering and stable business-key sorting.  All
classification arithmetic arrives already decided, so the serialized
report is byte-for-byte reproducible for the same ledger facts and
semantically equal external details, regardless of input order, hash
seed, locale or wall clock.
"""
from __future__ import annotations

from .amounts import format_signed_cents
from .contract import (
    DIFF_AMBIGUOUS,
    DIFF_AMOUNT_MISMATCH,
    DIFF_BASE_AMOUNT_MISMATCH,
    DIFF_CURRENCY_MISMATCH,
    DIFF_EXTERNAL_MISSING,
    DIFF_LEDGER_MISSING,
    DIFF_MATCHED,
    DIFF_CATEGORIES,
)
from .engine import AmbiguousComponent, PairedRecord, expected_base_cents
from .models import ExternalDetail, LedgerFact

__all__ = ["build_difference_sections", "build_summary", "classify_pair"]

_LEDGER_KEY_ORDER = (
    "fact_id",
    "voucher_id",
    "line_no",
    "voucher_ref",
    "date",
    "effective_date",
    "currency",
    "amount",
    "base_amount",
    "rate",
)
_RATE_KEY_ORDER = ("rate", "rate_date", "rate_source")
_EXTERNAL_KEY_ORDER = (
    "source_id",
    "voucher_ref",
    "date",
    "currency",
    "amount",
)
_PAIR_KEY_ORDER = (
    "category",
    "match_method",
    "external",
    "ledger",
    "external_amount",
    "ledger_amount",
    "expected_base_amount",
    "ledger_base_amount",
    "base_currency_difference",
)
_LEDGER_MISSING_KEY_ORDER = ("category", "external")
_EXTERNAL_MISSING_KEY_ORDER = ("category", "ledger")
_AMBIGUOUS_KEY_ORDER = ("category", "external", "ledger")


def _rate_doc(fact: LedgerFact) -> dict | None:
    if fact.rate is None:
        return None
    values = {
        "rate": fact.rate.rate,
        "rate_date": fact.rate.rate_date,
        "rate_source": fact.rate.rate_source,
    }
    return {key: values[key] for key in _RATE_KEY_ORDER}


def ledger_doc(fact: LedgerFact) -> dict:
    values = {
        "fact_id": fact.fact_id,
        "voucher_id": fact.voucher_id,
        "line_no": fact.line_no,
        "voucher_ref": fact.voucher_ref,
        "date": fact.business_date,
        "effective_date": fact.effective_date,
        "currency": fact.currency,
        "amount": format_signed_cents(fact.amount_cents),
        "base_amount": (
            format_signed_cents(fact.base_amount_cents)
            if fact.base_amount_cents is not None
            else None
        ),
        "rate": _rate_doc(fact),
    }
    return {key: values[key] for key in _LEDGER_KEY_ORDER}


def external_doc(detail: ExternalDetail) -> dict:
    values = {
        "source_id": detail.source_id,
        "voucher_ref": detail.voucher_ref,
        "date": detail.business_date,
        "currency": detail.currency,
        "amount": format_signed_cents(detail.amount_cents),
    }
    return {key: values[key] for key in _EXTERNAL_KEY_ORDER}


def classify_pair(
    pair: PairedRecord, base_currency: str
) -> tuple[str, int | None, int | None]:
    """Decide the category for an identity/attribute-confirmed pair.

    Returns ``(category, expected_base_cents_or_None,
    base_difference_cents_or_None)``.  Currency is compared first, then
    the original-currency amount, then the saved base-currency amount
    against the external amount converted at the ledger's *recorded*
    rate (never a freshly fetched one).
    """
    detail, fact = pair.external, pair.fact
    if detail.currency != fact.currency:
        return DIFF_CURRENCY_MISMATCH, None, None
    if detail.amount_cents != fact.amount_cents:
        return DIFF_AMOUNT_MISMATCH, None, None

    expected_base: int | None = None
    if detail.currency == base_currency:
        expected_base = detail.amount_cents
    elif fact.rate is not None:
        expected_base = expected_base_cents(
            detail.amount_cents, fact.rate.rate)

    base_difference: int | None = None
    if (expected_base is not None
            and fact.base_amount_cents is not None):
        base_difference = fact.base_amount_cents - expected_base
        if base_difference != 0:
            return (DIFF_BASE_AMOUNT_MISMATCH, expected_base,
                    base_difference)
    return DIFF_MATCHED, expected_base, base_difference


def _pair_item(
    pair: PairedRecord,
    category: str,
    expected_base: int | None,
    base_difference: int | None,
) -> dict:
    values = {
        "category": category,
        "match_method": pair.method,
        "external": external_doc(pair.external),
        "ledger": ledger_doc(pair.fact),
        "external_amount": {
            "currency": pair.external.currency,
            "amount": format_signed_cents(pair.external.amount_cents),
        },
        "ledger_amount": {
            "currency": pair.fact.currency,
            "amount": format_signed_cents(pair.fact.amount_cents),
        },
        "expected_base_amount": (
            format_signed_cents(expected_base)
            if expected_base is not None
            else None
        ),
        "ledger_base_amount": (
            format_signed_cents(pair.fact.base_amount_cents)
            if pair.fact.base_amount_cents is not None
            else None
        ),
        "base_currency_difference": (
            format_signed_cents(base_difference)
            if base_difference is not None
            else None
        ),
    }
    return {key: values[key] for key in _PAIR_KEY_ORDER}


def _pair_sort_key(item: dict) -> tuple:
    # Stable business key: external source id first, then the ledger fact
    # id; match method and input position never decide order.
    return (item["external"]["source_id"], item["ledger"]["fact_id"])


def _ledger_missing_item(detail: ExternalDetail) -> dict:
    values = {"category": DIFF_LEDGER_MISSING, "external": external_doc(detail)}
    return {key: values[key] for key in _LEDGER_MISSING_KEY_ORDER}


def _external_missing_item(fact: LedgerFact) -> dict:
    values = {"category": DIFF_EXTERNAL_MISSING, "ledger": ledger_doc(fact)}
    return {key: values[key] for key in _EXTERNAL_MISSING_KEY_ORDER}


def _ambiguous_item(component: AmbiguousComponent) -> dict:
    external = sorted(
        (external_doc(detail) for detail in component.externals),
        key=lambda doc: doc["source_id"],
    )
    ledger = sorted(
        (ledger_doc(fact) for fact in component.facts),
        key=lambda doc: doc["fact_id"],
    )
    values = {"category": DIFF_AMBIGUOUS, "external": external,
              "ledger": ledger}
    return {key: values[key] for key in _AMBIGUOUS_KEY_ORDER}


def build_difference_sections(
    pairs: tuple[PairedRecord, ...],
    ambiguous: tuple[AmbiguousComponent, ...],
    ledger_missing: tuple[ExternalDetail, ...],
    external_missing: tuple[LedgerFact, ...],
    base_currency: str,
) -> dict[str, list[dict]]:
    """Classify every record into the seven fixed category sections."""
    sections: dict[str, list[dict]] = {
        category: [] for category in DIFF_CATEGORIES
    }
    for pair in pairs:
        category, expected_base, base_difference = classify_pair(
            pair, base_currency)
        sections[category].append(
            _pair_item(pair, category, expected_base, base_difference))

    sections[DIFF_LEDGER_MISSING] = [
        _ledger_missing_item(detail) for detail in ledger_missing
    ]
    sections[DIFF_EXTERNAL_MISSING] = [
        _external_missing_item(fact) for fact in external_missing
    ]
    sections[DIFF_AMBIGUOUS] = [
        _ambiguous_item(component) for component in ambiguous
    ]

    for category in DIFF_CATEGORIES:
        if category == DIFF_LEDGER_MISSING:
            sections[category].sort(
                key=lambda item: item["external"]["source_id"])
        elif category == DIFF_EXTERNAL_MISSING:
            sections[category].sort(
                key=lambda item: (item["ledger"]["date"],
                                  item["ledger"]["fact_id"]))
        elif category == DIFF_AMBIGUOUS:
            sections[category].sort(
                key=lambda item: (
                    tuple(doc["source_id"] for doc in item["external"]),
                    tuple(doc["fact_id"] for doc in item["ledger"]),
                )
            )
        else:
            sections[category].sort(key=_pair_sort_key)

    return {category: sections[category] for category in DIFF_CATEGORIES}


def build_summary(
    facts: tuple[LedgerFact, ...],
    externals: tuple[ExternalDetail, ...],
    sections: dict[str, list[dict]],
    pairs: tuple[PairedRecord, ...],
    base_currency: str,
) -> dict:
    """Per-currency totals over both full populations, plus base gap.

    Original-currency amounts are summed over *every* in-scope ledger fact
    and *every* external detail, strictly per currency; amounts in
    different currencies are never added together.  Matched pairs land on
    both sides with equal amounts and therefore cancel; missing and
    mismatched records drive the per-currency ``difference`` (ledger minus
    external).  Ambiguous records are still summed, because each side's
    amounts are known even though the pairing is not unique.

    The single cross-currency figure ``base_currency_difference`` is
    assembled only where a saved or derivable base-currency amount exists:
    external-missing facts contribute their saved base amount, and a
    compared pair contributes the saved-minus-expected base gap.  A
    ledger-missing external detail with no recorded rate cannot be
    converted and so adds nothing (it is still counted in ``counts``).
    """
    ledger_by_currency: dict[str, int] = {}
    external_by_currency: dict[str, int] = {}

    def add(mapping: dict[str, int], currency: str, cents: int) -> None:
        mapping[currency] = mapping.get(currency, 0) + cents

    for fact in facts:
        add(ledger_by_currency, fact.currency, fact.amount_cents)
    for detail in externals:
        add(external_by_currency, detail.currency, detail.amount_cents)

    currencies = sorted(
        set(ledger_by_currency) | set(external_by_currency))
    by_currency: list[dict] = []
    for currency in currencies:
        ledger_cents = ledger_by_currency.get(currency, 0)
        external_cents = external_by_currency.get(currency, 0)
        row = {
            "currency": currency,
            "ledger_total": format_signed_cents(ledger_cents),
            "external_total": format_signed_cents(external_cents),
            "difference": format_signed_cents(
                ledger_cents - external_cents),
        }
        by_currency.append(row)

    base_difference = 0
    for pair in pairs:
        fact = pair.fact
        if fact.base_amount_cents is None:
            continue
        if pair.external.currency == base_currency:
            expected_base = pair.external.amount_cents
        elif fact.rate is not None:
            expected_base = expected_base_cents(
                pair.external.amount_cents, fact.rate.rate)
        else:
            continue
        base_difference += fact.base_amount_cents - expected_base
    for item in sections[DIFF_EXTERNAL_MISSING]:
        base_text = item["ledger"]["base_amount"]
        if base_text is not None:
            base_difference += _parse_rendered(base_text)

    counts = {
        category: len(sections[category]) for category in DIFF_CATEGORIES
    }
    return {
        "by_currency": by_currency,
        "base_currency": base_currency,
        "base_currency_difference": format_signed_cents(base_difference),
        "counts": {category: counts[category]
                   for category in DIFF_CATEGORIES},
    }


def _parse_rendered(text: str) -> int:
    """Parse back a rendered signed amount for pure integer summation."""
    from .amounts import parse_signed_cents

    return parse_signed_cents(text, where="summary")
