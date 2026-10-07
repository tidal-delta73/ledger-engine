"""Cutoff scoping, cancellation and deterministic matching engine.

This module is pure data-in/data-out: given parsed ledger facts, external
details, a cutoff day and the period table, it decides which facts are
effective, which are offset by an in-scope reversal, and how the two sides
pair up.  Nothing here performs a posting or reads the wall clock.

Matching order is contractual:

1. *Identity* confirms the same business first through either relation:
   the ledger fact carries the external source id as its ``voucher_ref``
   (``source_id`` method), or the external voucher reference names the
   fact's ``voucher_id`` (``voucher_ref`` method).
2. Attribute fallback (one-to-one on currency, signed amount and business
   date, with the account fixed by the request) is allowed *only* for
   records that carry no business identity of their own and took part in
   no identity relation.  A record that asserts an identity which resolves
   to nothing is a plain missing record, never an attribute guess.
3. Any decision that is not unique -- an identity component fans out, or
   an attribute bucket is not 1:1 -- becomes one *ambiguous* item covering
   every record in that component; a candidate is never picked by input
   order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .contract import MATCH_ATTRIBUTES, MATCH_SOURCE_ID, MATCH_VOUCHER_REF
from .models import ExternalDetail, LedgerFact

__all__ = [
    "PairedRecord",
    "AmbiguousComponent",
    "effective_facts",
    "expected_base_cents",
    "pair_records",
]

_QUANTUM = Decimal(1)


@dataclass(frozen=True)
class PairedRecord:
    """An external detail paired with exactly one ledger fact."""

    external: ExternalDetail
    fact: LedgerFact
    method: str


@dataclass(frozen=True)
class AmbiguousComponent:
    """A non-unique decision: every related record on both sides."""

    externals: tuple[ExternalDetail, ...]
    facts: tuple[LedgerFact, ...]


def expected_base_cents(amount_cents: int, rate_text: str) -> int:
    """Convert signed foreign-currency cents at the *recorded* rate.

    The rate is the one stored on the historical fact, never a rate
    fetched at call time.  Because both sides are held in integer cents,
    the cent count times the rate is already the base-currency cent
    count (``major*100 * rate = major*rate*100``); rounding is fixed
    ROUND_HALF_UP to integer cents via :class:`decimal.Decimal`, with no
    float at any magnitude.
    """
    value = (Decimal(amount_cents) * Decimal(rate_text)).quantize(
        _QUANTUM, rounding=ROUND_HALF_UP)
    return int(value)


def effective_facts(
    facts: tuple[LedgerFact, ...],
    account_code: str,
    cutoff: date,
) -> tuple[LedgerFact, ...]:
    """Keep facts in-scope, effective by cutoff and not fully offset.

    A fact qualifies when its account matches and ``effective_date`` is
    not later than the cutoff.  It is then dropped only when *fully*
    cancelled by a counterpart that is itself effective by the cutoff:

    * a symmetric ``offsets`` pair whose in-scope signed offsets sum to
      the fact's exact negation (both sides of the pair are dropped); or
    * a ``reversed_by`` counterpart present in scope whose amount is the
      exact negation on the same currency (both facts are dropped).

    A reversal recorded after the cutoff cannot erase history, and a
    partially offset fact survives with its originally recorded amount
    untouched (no historical amount is rewritten).
    """
    in_scope = tuple(
        fact
        for fact in facts
        if fact.account_code == account_code
        and date.fromisoformat(fact.effective_date) <= cutoff
    )
    by_id = {fact.fact_id: fact for fact in in_scope}

    cancelled: set[str] = set()
    for fact in in_scope:
        offset_total = 0
        referenced: list[str] = []
        for offset in fact.offsets:
            if offset.fact_id in by_id:
                offset_total += offset.amount_cents
                referenced.append(offset.fact_id)
        if offset_total != 0 and fact.amount_cents + offset_total == 0:
            cancelled.add(fact.fact_id)
            cancelled.update(referenced)
        for reverser_id in fact.reversed_by:
            reverser = by_id.get(reverser_id)
            if (reverser is not None
                    and reverser.currency == fact.currency
                    and reverser.amount_cents == -fact.amount_cents):
                cancelled.add(fact.fact_id)
                cancelled.add(reverser_id)

    return tuple(fact for fact in in_scope if fact.fact_id not in cancelled)


def _identity_edges(
    externals: tuple[ExternalDetail, ...],
    facts: tuple[LedgerFact, ...],
) -> set[tuple[int, int, str]]:
    """All confirmed identity edges ``(external index, fact index, method)``.

    Two independent relations count: the ledger fact carries the external
    source id in ``voucher_ref`` (``source_id``), or the external voucher
    reference names the fact's ``voucher_id`` (``voucher_ref``).
    """
    edges: set[tuple[int, int, str]] = set()
    for ei, detail in enumerate(externals):
        for fi, fact in enumerate(facts):
            if (fact.voucher_ref is not None
                    and fact.voucher_ref == detail.source_id):
                edges.add((ei, fi, MATCH_SOURCE_ID))
            if (detail.voucher_ref is not None
                    and fact.voucher_id is not None
                    and fact.voucher_id == detail.voucher_ref):
                edges.add((ei, fi, MATCH_VOUCHER_REF))
    return edges


def _components(node_count: int, edges: set[tuple[int, int]]) -> list[set[int]]:
    """Connected components of an undirected graph on integer nodes."""
    adjacency: dict[int, set[int]] = {node: set() for node in range(node_count)}
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    visited: set[int] = set()
    components: list[set[int]] = []
    for start in range(node_count):
        if start in visited:
            continue
        group: set[int] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in group:
                continue
            group.add(node)
            visited.add(node)
            stack.extend(adjacency[node] - group)
        components.append(group)
    return components


def pair_records(
    externals: tuple[ExternalDetail, ...],
    facts: tuple[LedgerFact, ...],
) -> tuple[
    tuple[PairedRecord, ...],
    tuple[AmbiguousComponent, ...],
    tuple[ExternalDetail, ...],
    tuple[LedgerFact, ...],
]:
    """Pair external details with facts deterministically.

    Returns ``(pairs, ambiguous, ledger_missing, external_missing)``:
    external-without-ledger singletons and ledger-without-external
    singletons respectively.
    """
    edge_triples = _identity_edges(externals, facts)
    ext_count = len(externals)
    graph_edges = {(ei, ext_count + fi) for ei, fi, _m in edge_triples}
    edge_method: dict[tuple[int, int], str] = {}
    for ei, fi, method in edge_triples:
        previous = edge_method.get((ei, fi))
        if previous is None or (
                previous == MATCH_VOUCHER_REF and method == MATCH_SOURCE_ID):
            edge_method[(ei, fi)] = method

    ambiguous: list[AmbiguousComponent] = []
    paired: list[PairedRecord] = []
    identity_ext: set[int] = set()
    identity_fact: set[int] = set()

    for component in _components(ext_count + len(facts), graph_edges):
        ext_nodes = sorted(n for n in component if n < ext_count)
        fact_nodes = sorted(n - ext_count for n in component if n >= ext_count)
        if not ext_nodes or not fact_nodes:
            continue  # truly isolated nodes have no identity edges
        identity_ext.update(ext_nodes)
        identity_fact.update(fact_nodes)
        if len(ext_nodes) == 1 and len(fact_nodes) == 1:
            ei, fi = ext_nodes[0], fact_nodes[0]
            paired.append(
                PairedRecord(externals[ei], facts[fi],
                             edge_method[(ei, fi)]))
        else:
            ambiguous.append(
                AmbiguousComponent(
                    externals=tuple(externals[i] for i in ext_nodes),
                    facts=tuple(facts[i] for i in fact_nodes),
                )
            )

    # Records with no identity edge at all.  Those that *assert* an
    # identity which resolves to nothing are missing records; only records
    # without any asserted identity may use the attribute fallback.
    orphan_externals = [
        i for i in range(ext_count) if i not in identity_ext
    ]
    orphan_facts = [
        i for i in range(len(facts)) if i not in identity_fact
    ]
    unresolved_ext = [
        i for i in orphan_externals if externals[i].voucher_ref is not None
    ]
    unresolved_fact = [
        i for i in orphan_facts if facts[i].voucher_ref is not None
    ]
    fallback_ext = [
        i for i in orphan_externals if externals[i].voucher_ref is None
    ]
    fallback_fact = [
        i for i in orphan_facts if facts[i].voucher_ref is None
    ]

    # One-to-one attribute buckets on (currency, signed amount, date);
    # the account is fixed by the request, so it is not part of the key.
    buckets: dict[tuple[str, int, str], dict[str, list[int]]] = {}
    for ei in fallback_ext:
        detail = externals[ei]
        key = (detail.currency, detail.amount_cents, detail.business_date)
        buckets.setdefault(key, {"ext": [], "fact": []})["ext"].append(ei)
    for fi in fallback_fact:
        fact = facts[fi]
        key = (fact.currency, fact.amount_cents, fact.business_date)
        buckets.setdefault(key, {"ext": [], "fact": []})["fact"].append(fi)

    matched_ext: set[int] = set()
    matched_fact: set[int] = set()
    for bucket in buckets.values():
        ext_list, fact_list = bucket["ext"], bucket["fact"]
        if len(ext_list) == 1 and len(fact_list) == 1:
            paired.append(
                PairedRecord(externals[ext_list[0]], facts[fact_list[0]],
                             MATCH_ATTRIBUTES))
            matched_ext.add(ext_list[0])
            matched_fact.add(fact_list[0])
        elif ext_list and fact_list:
            # Both populated but not uniquely 1:1 -> ambiguous.
            ambiguous.append(
                AmbiguousComponent(
                    externals=tuple(externals[i] for i in ext_list),
                    facts=tuple(facts[i] for i in fact_list),
                )
            )
            matched_ext.update(ext_list)
            matched_fact.update(fact_list)

    ledger_missing_indexes = sorted(
        set(unresolved_ext) | (set(fallback_ext) - matched_ext),
        key=lambda i: externals[i].source_id,
    )
    external_missing_indexes = sorted(
        set(unresolved_fact) | (set(fallback_fact) - matched_fact),
        key=lambda i: facts[i].fact_id,
    )
    return (
        tuple(paired),
        tuple(ambiguous),
        tuple(externals[i] for i in ledger_missing_indexes),
        tuple(facts[i] for i in external_missing_indexes),
    )
