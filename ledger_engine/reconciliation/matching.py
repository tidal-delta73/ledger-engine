"""Deterministic matching between ledger facts and external lines.

Identity is resolved in three strictly ordered stages.

1. **Strong source identity.**  An external line's source id is the
   unique id within the external source.  A ledger fact that echoes that
   id (``source_ref``) confirms the business.  One line + one fact with
   the same id is a confirmed pair; one id echoed by several facts is an
   ambiguity.  A fact carrying a source id no external line echoes is a
   dangling identifier -- it becomes "missing externally" and may never
   attribute-match.

2. **Voucher-reference identity.**  Only lines without a strong echo may
   use ``voucher_ref``.  On the subgraph of such lines and facts with no
   source id, voucher edges (``voucher_ref == voucher_id``) are split
   into connected components.  A one-line/one-fact component confirms a
   pair; any larger component is an ambiguity; a line whose voucher
   points at nothing is a dangling reference ("missing on the books")
   and likewise skips the attribute stage.  Strongly resolved facts are
   removed from this subgraph, so an already-identified voucher line can
   never drag a sibling line into ambiguity -- source identity wins.

3. **Attribute fallback.**  Records still without any identifier --
   lines with neither an echo nor a voucher reference, and facts with no
   source id and no voucher edge -- may be paired one-to-one on
   ``(business date, currency, original amount)``; the account is fixed
   by the call.  A bucket with exactly one fact and one line confirms;
   an empty side is a plain "missing"; both sides present with any
   multiplicity is an ambiguity.

Every record lands in exactly one result collection.  The partition is a
pure function of record content: it never depends on input order.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import ExternalLine, LedgerFact
from .money import BASE_SCALE, precision_for, round_half_up

__all__ = [
    "ConfirmedPair",
    "AmbiguityGroup",
    "MatchResult",
    "match_records",
    "external_effective_base",
]

REASON_REFERENCE = "non_unique_reference_candidates"
REASON_ATTRIBUTE = "non_unique_attribute_candidates"


@dataclass(frozen=True)
class ConfirmedPair:
    """One external line confirmed as the same business as one fact."""

    fact: LedgerFact
    line: ExternalLine


@dataclass(frozen=True)
class AmbiguityGroup:
    """A set of records whose one-to-one identity cannot be resolved."""

    facts: tuple[LedgerFact, ...]
    lines: tuple[ExternalLine, ...]
    reason: str


@dataclass(frozen=True)
class MatchResult:
    """The complete classification of the in-scope record population.

    Every input record occurs exactly once across the four collections.
    """

    pairs: tuple[ConfirmedPair, ...]
    missing_on_books: tuple[ExternalLine, ...]
    missing_externally: tuple[LedgerFact, ...]
    ambiguities: tuple[AmbiguityGroup, ...]


def external_effective_base(line: ExternalLine) -> int | None:
    """Base-currency integer cents claimed by the external side, if any.

    A direct ``base_amount`` wins.  Otherwise the optionally recorded
    rate is applied to the original amount exactly and rounded half-up
    to the fixed two-place base precision.  With neither, the external
    side makes no base-currency claim (``None``); no current rate is
    ever substituted.
    """
    if line.base_minor is not None:
        return line.base_minor
    if line.recorded_rate is None:
        return None
    original_scale = 10 ** precision_for(line.currency)
    rate = line.recorded_rate
    return round_half_up(
        line.original_minor * BASE_SCALE * rate.numerator,
        original_scale * rate.denominator,
    )


def match_records(
    facts: list[LedgerFact], lines: list[ExternalLine]
) -> MatchResult:
    """Classify every in-scope fact/line exactly once.

    Pure and order-independent: input ordering never influences a
    pairing or a grouping; only immutable record content does.
    """
    pairs: list[ConfirmedPair] = []
    ambiguities: list[AmbiguityGroup] = []
    dangling_facts: list[LedgerFact] = []
    dangling_lines: list[ExternalLine] = []

    lines_by_id = {line.source_id: line for line in lines}
    facts_echoing: dict[str, list[LedgerFact]] = {}
    for fact in facts:
        if fact.source_ref is not None:
            facts_echoing.setdefault(fact.source_ref, []).append(fact)

    # Stage 1: strong source-id identity.
    resolved_fact_ids: set[str] = set()
    resolved_line_ids: set[str] = set()
    for source_id in sorted(facts_echoing):
        echo_facts = sorted(facts_echoing[source_id],
                            key=lambda item: item.business_key())
        line = lines_by_id.get(source_id)
        if line is None:
            # The books assert an external id the snapshot does not know.
            for fact in echo_facts:
                dangling_facts.append(fact)
                resolved_fact_ids.add(fact.entry_id)
            continue
        resolved_line_ids.add(line.source_id)
        if len(echo_facts) == 1:
            pairs.append(ConfirmedPair(fact=echo_facts[0], line=line))
            resolved_fact_ids.add(echo_facts[0].entry_id)
        else:
            ambiguities.append(AmbiguityGroup(
                facts=tuple(echo_facts), lines=(line,),
                reason=REASON_REFERENCE,
            ))
            for fact in echo_facts:
                resolved_fact_ids.add(fact.entry_id)

    # Stage 2: voucher-reference identity over unresolved records only.
    weak_lines = [
        line for line in lines
        if line.source_id not in resolved_line_ids and line.voucher_ref
    ]
    weak_facts = [
        fact for fact in facts
        if fact.entry_id not in resolved_fact_ids and fact.source_ref is None
    ]

    facts_by_voucher: dict[str, list[LedgerFact]] = {}
    for fact in weak_facts:
        facts_by_voucher.setdefault(fact.voucher_id, []).append(fact)

    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for fact in weak_facts:
        adjacency[("F", fact.entry_id)] = set()
    for line in weak_lines:
        adjacency[("L", line.source_id)] = set()
    weak_facts_by_id = {fact.entry_id: fact for fact in weak_facts}
    weak_lines_by_id = {line.source_id: line for line in weak_lines}
    for line in weak_lines:
        line_node = ("L", line.source_id)
        for fact in facts_by_voucher.get(line.voucher_ref, ()):
            fact_node = ("F", fact.entry_id)
            adjacency[line_node].add(fact_node)
            adjacency[fact_node].add(line_node)

    seen: set[tuple[str, str]] = set()
    weak_resolved_fact_ids: set[str] = set()
    weak_resolved_line_ids: set[str] = set()
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack = [start]
        component: set[tuple[str, str]] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.add(node)
            stack.extend(sorted(adjacency[node] - seen))

        fact_nodes = sorted(node for node in component if node[0] == "F")
        line_nodes = sorted(node for node in component if node[0] == "L")
        if len(fact_nodes) == 1 and len(line_nodes) == 1:
            pairs.append(ConfirmedPair(
                fact=weak_facts_by_id[fact_nodes[0][1]],
                line=weak_lines_by_id[line_nodes[0][1]],
            ))
            weak_resolved_fact_ids.add(fact_nodes[0][1])
            weak_resolved_line_ids.add(line_nodes[0][1])
            continue
        if len(component) == 1:
            (kind, ident), = component
            if kind == "L":
                # Voucher reference asserted but no fact carries it.
                dangling_lines.append(weak_lines_by_id[ident])
                weak_resolved_line_ids.add(ident)
            # A singleton fact has no inbound voucher edge and simply
            # proceeds to the attribute stage below.
            continue
        group_facts = tuple(weak_facts_by_id[node[1]] for node in fact_nodes)
        group_lines = tuple(weak_lines_by_id[node[1]] for node in line_nodes)
        ambiguities.append(AmbiguityGroup(
            facts=group_facts, lines=group_lines, reason=REASON_REFERENCE,
        ))
        weak_resolved_fact_ids.update(node[1] for node in fact_nodes)
        weak_resolved_line_ids.update(node[1] for node in line_nodes)

    # Stage 3: attribute fallback over identifier-free records.
    fallback_facts = [
        fact for fact in weak_facts
        if fact.entry_id not in weak_resolved_fact_ids
    ]
    fallback_lines = [
        line for line in lines
        if line.source_id not in resolved_line_ids
        and line.source_id not in weak_resolved_line_ids
        and line.voucher_ref is None
    ]

    buckets: dict[
        tuple[str, str, int],
        tuple[list[LedgerFact], list[ExternalLine]],
    ] = {}
    for fact in fallback_facts:
        buckets.setdefault((fact.date, fact.currency, fact.original_minor),
                           ([], []))[0].append(fact)
    for line in fallback_lines:
        buckets.setdefault(
            (line.business_date, line.currency, line.original_minor),
            ([], []),
        )[1].append(line)

    attribute_missing_facts: list[LedgerFact] = []
    attribute_missing_lines: list[ExternalLine] = []
    for key in sorted(buckets):
        bucket_facts, bucket_lines = buckets[key]
        if len(bucket_facts) == 1 and len(bucket_lines) == 1:
            pairs.append(ConfirmedPair(fact=bucket_facts[0],
                                       line=bucket_lines[0]))
        elif not bucket_facts or not bucket_lines:
            attribute_missing_facts.extend(bucket_facts)
            attribute_missing_lines.extend(bucket_lines)
        else:
            ambiguities.append(AmbiguityGroup(
                facts=tuple(sorted(bucket_facts,
                                   key=lambda item: item.business_key())),
                lines=tuple(sorted(bucket_lines,
                                   key=lambda item: item.business_key())),
                reason=REASON_ATTRIBUTE,
            ))

    return MatchResult(
        pairs=tuple(pairs),
        missing_on_books=tuple(dangling_lines + attribute_missing_lines),
        missing_externally=tuple(dangling_facts
                                 + attribute_missing_facts),
        ambiguities=tuple(ambiguities),
    )
