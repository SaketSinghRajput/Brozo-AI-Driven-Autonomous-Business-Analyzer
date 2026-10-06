"""SemanticMapper (TASK-SEM-006; design 15, TRD 13.3): Understanding + domain -> SemanticModel.

1. Packs in play: the primary domain (and MULTI_DOMAIN partners) plus ``generic``; UNKNOWN or
   AMBIGUOUS domains use ``generic`` only (CLAUDE.md section 11: generic analysis).
2. Candidates per column slot (entity key / measure / time / dimension): concepts of the same
   kind whose semantic types include the column's type, scored
   ``w_alias*alias + w_type*1 + w_unit*unit_compat + w_pack*pack_weight``; confirmed prior
   mappings score 1.0. Generic concepts get an alias floor (they match by type).
3. Greedy assignment, one concept per column and one column per concept. If another column
   scores within delta for the same concept, both are marked ``MAPPING_AMBIGUOUS`` and neither
   gets it, so no metric formula uses the concept until a user confirms.
4. Unmapped columns stay usable under their own (title-cased) names.
5. Pack metric templates are instantiated only when their concepts are mapped (metrics.py).
Overrides: previous overrides are carried to the new version here; confirmed mappings arrive as
``prior`` and the other overrides are re-applied by ``app.data.semantic.overrides.remap``.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.data.domain_packs.loader import default_packs
from app.data.domain_packs.model import Concept, ConceptKind, DomainPack
from app.data.semantic.aliases import best_alias
from app.data.semantic.domain import (
    Aggregation,
    SemanticDimension,
    SemanticEntity,
    SemanticGraph,
    SemanticMeasure,
    SemanticModel,
)
from app.data.semantic.graph import EdgeKind, GraphBuilder, node_id
from app.data.semantic.metrics import MetricResult, instantiate_metrics
from app.data.semantic.typecheck import Symbol
from app.data.understanding.config import UnderstandingConfig, default_config
from app.data.understanding.domain import (
    DomainClassification,
    DomainStatus,
    Measure,
    SemanticType,
    Understanding,
)
from app.data.understanding.scoring import weighted_score
from app.data.understanding.text import title, tokens

GENERIC = "generic"
GENERIC_ALIAS_FLOOR = 0.6
_CURRENCY_TYPES = frozenset({SemanticType.CURRENCY_AMOUNT, SemanticType.PRICE})
_CONFIRMED = frozenset({DomainStatus.KNOWN, DomainStatus.MULTI_DOMAIN})


@dataclass(frozen=True)
class Slot:
    column: str
    kind: ConceptKind
    semantic_type: SemanticType
    unit: str | None = None


@dataclass(frozen=True)
class Candidate:
    concept: Concept
    pack: DomainPack
    score: float
    alias: str | None
    from_prior: bool = False


@dataclass(frozen=True)
class ColumnMapping:
    column: str
    kind: ConceptKind
    concept: str | None
    pack: str | None
    score: float
    ambiguous: bool = False
    alternatives: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MappingResult:
    model: SemanticModel
    mappings: list[ColumnMapping]
    review_flags: list[str]
    skipped_metrics: dict[str, str]


class SemanticMapper:
    def __init__(
        self,
        packs: Mapping[str, DomainPack] | None = None,
        config: UnderstandingConfig | None = None,
    ) -> None:
        self._packs = dict(packs if packs is not None else default_packs())
        self._cfg = (config or default_config()).mapping

    def packs_in_play(self, domain: DomainClassification) -> list[tuple[DomainPack, str]]:
        chosen: list[tuple[DomainPack, str]] = []
        if domain.status in _CONFIRMED and domain.primary:
            primary = self._packs[domain.primary]
            chosen.append((primary, "primary"))
            if domain.status is DomainStatus.MULTI_DOMAIN:
                chosen += [
                    (self._packs[c.domain], "secondary")
                    for c in domain.candidates[1:]
                    if c.domain in primary.compatible_with
                ]
        if GENERIC in self._packs:
            chosen.append((self._packs[GENERIC], "generic"))
        return chosen

    def map(
        self,
        understanding: Understanding,
        domain: DomainClassification,
        *,
        table: str,
        prior: Mapping[str, str] | None = None,
        previous: SemanticModel | None = None,
    ) -> MappingResult:
        packs = self.packs_in_play(domain)
        slots = _slots(understanding)
        candidates = {s.column: self._candidates(s, packs, prior or {}) for s in slots}
        assigned, ambiguous = self._assign(slots, candidates)
        flags = [] if domain.status in _CONFIRMED else ["DOMAIN_UNCERTAIN"]
        flags += [f"MAPPING_AMBIGUOUS:{c}" for c in sorted(ambiguous)]
        flags += [f"TIME_GRAIN_UNKNOWN:{t.column}" for t in understanding.time_dimensions
                  if t.grain is None]  # fmt: skip
        mappings = []
        for s in slots:
            hit = assigned.get(s.column)
            mappings.append(
                ColumnMapping(
                    column=s.column,
                    kind=s.kind,
                    concept=hit.concept.name if hit else None,
                    pack=hit.pack.id if hit else None,
                    score=hit.score if hit else 0.0,
                    ambiguous=s.column in ambiguous,
                    alternatives=[c.concept.name for c in candidates[s.column]][:3],
                )
            )
        model, skipped = self._build(understanding, table, packs, assigned, previous)
        return MappingResult(model, mappings, flags, skipped)

    def _candidates(
        self, slot: Slot, packs: Sequence[tuple[DomainPack, str]], prior: Mapping[str, str]
    ) -> list[Candidate]:
        out = []
        confirmed = prior.get(slot.column)
        for pack, role in packs:
            for concept in pack.concepts:
                if concept.kind != slot.kind or slot.semantic_type not in concept.semantic_types:
                    continue
                if confirmed == concept.name:
                    out.append(Candidate(concept, pack, 1.0, None, from_prior=True))
                    continue
                alias_score, alias = best_alias(slot.column, [concept.name, *concept.aliases])
                if pack.id == GENERIC:
                    alias_score = max(alias_score, GENERIC_ALIAS_FLOOR)
                signals = {
                    "alias": alias_score,
                    "type": 1.0,
                    "unit": _unit_compat(concept, slot.unit),
                    "pack": self._cfg.pack_weights.get(role, 0.0),
                }
                score = round(weighted_score(signals, self._cfg.weights), 4)
                if score >= self._cfg.tau:
                    out.append(Candidate(concept, pack, score, alias))
        out.sort(key=lambda c: (-c.score, not c.from_prior, c.concept.name))
        return out

    def _assign(
        self, slots: Sequence[Slot], candidates: Mapping[str, list[Candidate]]
    ) -> tuple[dict[str, Candidate], set[str]]:
        order = {s.column: i for i, s in enumerate(slots)}
        pairs = [(col, cand) for col, cands in candidates.items() for cand in cands]
        pairs.sort(key=lambda p: (-p[1].score, not p[1].from_prior, order[p[0]], p[1].concept.name))
        assigned: dict[str, Candidate] = {}
        ambiguous: set[str] = set()
        taken: set[str] = set()
        for column, cand in pairs:
            name = cand.concept.name
            if column in assigned or column in ambiguous or name in taken:
                continue
            rivals = [
                other
                for other, rival in pairs
                if rival.concept.name == name
                and other != column
                and other not in assigned
                and other not in ambiguous
                and cand.score - rival.score < self._cfg.delta
            ]
            taken.add(name)
            if rivals and not cand.from_prior:
                ambiguous.update([column, *rivals])  # concept blocked until a user confirms
            else:
                assigned[column] = cand
        return assigned, ambiguous

    def _build(
        self,
        u: Understanding,
        table: str,
        packs: Sequence[tuple[DomainPack, str]],
        assigned: Mapping[str, Candidate],
        previous: SemanticModel | None,
    ) -> tuple[SemanticModel, dict[str, str]]:
        names = _Names()
        id_dims = {d.column for d in u.dimensions if d.type == "identifier"}
        entities: list[SemanticEntity] = []
        primary: str | None = None
        for e in u.entities:
            hit = assigned.get(e.key_columns[0]) if len(e.key_columns) == 1 else None
            name = names.take("entity", hit.concept.name if hit else e.name)
            if primary is None and not set(e.key_columns) <= id_dims:
                primary = name
            entities.append(
                SemanticEntity(
                    name=name,
                    key=",".join(e.key_columns),
                    source_table=table,
                    confidence=hit.score if hit else e.confidence,
                )
            )
        measures = [_measure(m, u, assigned.get(m.column), names) for m in u.measures]
        dimensions = [
            SemanticDimension(
                name=names.take("dimension", _name(t.column, assigned)),
                column=t.column,
                type="time",
                entity=primary,
                grain=t.grain,
            )
            for t in u.time_dimensions
            if t.grain is not None
        ]
        dimensions += [
            SemanticDimension(
                name=names.take("dimension", _name(d.column, assigned)),
                column=d.column,
                type="geo" if d.type == "geo" else "boolean" if d.type == "boolean" else "category",
                entity=primary,
            )
            for d in u.dimensions
            if d.type != "identifier"
        ]
        symbols = {("measure", m.name): Symbol("number", m.unit, m.additivity) for m in measures}
        symbols |= {("dimension_key", e.name): Symbol("key") for e in entities}
        for d in dimensions:
            symbols[("dimension", d.name)] = Symbol("time" if d.type == "time" else "category")
        mapped = frozenset(c.concept.name for c in assigned.values())
        result = instantiate_metrics([p for p, _ in packs], mapped, symbols)
        version = previous.version + 1 if previous else 1
        model = SemanticModel(
            semantic_model_id=_model_id(u.dataset_version_id, version),
            dataset_version_id=u.dataset_version_id,
            version=version,
            entities=entities,
            dimensions=dimensions,
            measures=measures,
            metrics=result.metrics,
            graph=_graph(table, packs, entities, primary, measures, dimensions, result),
            overrides=list(previous.overrides) if previous else [],
        )
        return model, result.skipped


def _graph(
    table: str,
    packs: Sequence[tuple[DomainPack, str]],
    entities: Sequence[SemanticEntity],
    primary: str | None,
    measures: Sequence[SemanticMeasure],
    dimensions: Sequence[SemanticDimension],
    metrics: MetricResult,
) -> SemanticGraph:
    graph = GraphBuilder()
    for e in entities:
        graph.node("entity", e.name)
    pack_edges = [(e.from_, e.to, e.verb) for p, _ in packs for e in p.entity_graph]
    for e in entities:
        if primary is None or e.name == primary:
            continue
        match = next(((a, b, v) for a, b, v in pack_edges if {a, b} == {primary, e.name}), None)
        src, dst, verb = match or (primary, e.name, "references")
        evidence = f"{table}.{e.key} references {e.name}"
        graph.edge(node_id("entity", src), node_id("entity", dst), verb, e.confidence, evidence)
    anchor = node_id("entity", primary) if primary else None
    for m in measures:
        nid = graph.node("measure", m.name)
        if anchor:
            graph.edge(nid, anchor, "measured_on", m.confidence, f"{table}.{m.column}")
    for d in dimensions:
        nid = graph.node("dimension", d.name)
        if anchor:
            graph.edge(nid, anchor, "attribute_of", 1.0, f"{table}.{d.column}")
    for metric in metrics.metrics:
        graph.node("metric", metric.name)
    derived: EdgeKind = "derived_from"
    for metric in metrics.metrics:
        source = node_id("metric", metric.name)
        for kind, name in metrics.derived_from[metric.name]:
            if kind == "measure":
                graph.edge(source, node_id("measure", name), derived, 1.0, metric.source or "")
            elif kind == "metric":
                graph.edge(source, node_id("metric", name), derived, 1.0, metric.source or "")
    return graph.build()


def _measure(m: Measure, u: Understanding, hit: Candidate | None, names: _Names) -> SemanticMeasure:
    column = next(c for c in u.columns if c.name == m.column)
    name = names.take("measure", hit.concept.name if hit else title(tokens(m.column)))
    return SemanticMeasure(
        name=name,
        column=m.column,
        unit=m.unit,
        additivity=m.additivity,
        default_agg=_default_agg(m),
        confidence=hit.score if hit else column.confidence,
        aliases=[hit.alias] if hit and hit.alias and hit.alias != name else [],
    )


class _Names:
    """Unique names per kind; a clash gets a numeric suffix."""

    def __init__(self) -> None:
        self._used: dict[str, set[str]] = {}

    def take(self, kind: str, name: str) -> str:
        used = self._used.setdefault(kind, set())
        candidate, n = name, 2
        while candidate in used:
            candidate, n = f"{name} {n}", n + 1
        used.add(candidate)
        return candidate


def _name(column: str, assigned: Mapping[str, Candidate]) -> str:
    hit = assigned.get(column)
    return hit.concept.name if hit else title(tokens(column))


def _default_agg(m: Measure) -> Aggregation:
    # semi-additive: SUM across dimensions; the compiler applies LAST over time (TRD 11.4)
    return "sum" if m.additivity in {"additive", "semi_additive"} else "avg"


def _unit_compat(concept: Concept, unit: str | None) -> float:
    if concept.kind != "measure":
        return 1.0
    wants_currency = bool(_CURRENCY_TYPES & set(concept.semantic_types))
    has_currency = unit is not None and unit.startswith("currency")
    return 1.0 if wants_currency == has_currency else 0.3


def _slots(u: Understanding) -> list[Slot]:
    types = {c.name: c.semantic_type for c in u.columns}
    slots: list[Slot] = []
    seen: set[str] = set()
    candidates: list[Slot] = [
        Slot(e.key_columns[0], "entity", types[e.key_columns[0]])
        for e in u.entities
        if len(e.key_columns) == 1
    ]
    candidates += [Slot(m.column, "measure", types[m.column], m.unit) for m in u.measures]
    candidates += [Slot(t.column, "time", types[t.column]) for t in u.time_dimensions]
    candidates += [
        Slot(d.column, "dimension", types[d.column]) for d in u.dimensions if d.type != "identifier"
    ]
    for slot in candidates:
        if slot.column not in seen:
            seen.add(slot.column)
            slots.append(slot)
    return slots


def _model_id(dataset_version_id: str, version: int) -> str:
    digest = hashlib.sha256(f"{dataset_version_id}:{version}".encode()).hexdigest()
    return f"sm_{digest[:26]}"
