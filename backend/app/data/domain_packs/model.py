"""Domain pack format (TASK-DOMAIN-001; spec: docs/domain-packs.md).

A pack is data: lexicon terms, business concepts with aliases, signatures (concept sets that
characterise the domain), an entity graph, statistical hints and KPI metric templates. New domains
are added as files, never as code (PRD-NFR-003). Cross-references are validated here; metric
template expressions are parsed + type-checked by the loader.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.data.understanding.domain import Additivity, SemanticType

PackId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")]
Name = Annotated[str, Field(min_length=1, max_length=64)]
ConceptKind = Literal["entity", "measure", "dimension", "time"]


class _Pack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LexiconTerm(_Pack):
    term: Annotated[str, Field(min_length=1, max_length=64)]  # header phrase, tokenised on match
    weight: Annotated[float, Field(gt=0.0, le=1.0)]


class Concept(_Pack):
    name: Name
    kind: ConceptKind
    semantic_types: Annotated[list[SemanticType], Field(min_length=1)]
    aliases: Annotated[list[str], Field(min_length=1)]
    unique: bool = False  # at most one column may map to it (e.g. Revenue)
    description: str = ""


class Signature(_Pack):
    id: PackId
    description: str
    requires: Annotated[list[Name], Field(min_length=1)]  # concept names, all needed
    weight: Annotated[float, Field(gt=0.0)] = 1.0


class EntityEdge(_Pack):
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_by_name=True, validate_by_alias=True
    )

    from_: Annotated[Name, Field(alias="from")]
    to: Name
    verb: Literal["places", "contains", "belongs_to", "references"]


class StatHint(_Pack):
    id: PackId
    description: str
    semantic_type: SemanticType
    additivity: Additivity | None = None
    weight: Annotated[float, Field(gt=0.0)] = 1.0


class MetricTemplate(_Pack):
    name: Name
    expr: Annotated[str, Field(min_length=1, max_length=2000)]
    requires: Annotated[list[Name], Field(min_length=1)]  # concept names that must be mapped
    unit: str | None = None
    direction: Literal["higher_is_better", "lower_is_better", "neutral"] = "neutral"
    description: str = ""
    source: PackId  # id of an entry in ``sources`` (every KPI cites a definition)


class Source(_Pack):
    id: PackId
    title: str
    url: HttpUrl


class DomainPack(_Pack):
    schema_version: Literal["1.0"] = "1.0"
    id: PackId
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    name: Name
    description: str
    scorable: bool = True  # false for the generic fallback pack
    compatible_with: list[PackId] = []  # packs that may co-occur (MULTI_DOMAIN)
    lexicon: list[LexiconTerm] = []
    concepts: list[Concept]
    signatures: list[Signature] = []
    entity_graph: list[EntityEdge] = []
    stat_hints: list[StatHint] = []
    metrics: list[MetricTemplate] = []
    sources: list[Source] = []

    @property
    def ref(self) -> str:
        return f"pack:{self.id}@{self.version}"

    def concept(self, name: str) -> Concept | None:
        return next((c for c in self.concepts if c.name == name), None)

    @model_validator(mode="after")
    def _references(self) -> Self:
        concepts = {c.name: c for c in self.concepts}
        _unique("concept", [c.name for c in self.concepts])
        _unique("metric", [m.name for m in self.metrics])
        _unique("signature", [s.id for s in self.signatures])
        _unique("lexicon term", [t.term for t in self.lexicon])
        for sig in self.signatures:
            _known(f"signature {sig.id!r}", sig.requires, concepts)
        for edge in self.entity_graph:
            for end in (edge.from_, edge.to):
                if end not in concepts or concepts[end].kind != "entity":
                    raise ValueError(f"entity_graph endpoint {end!r} is not an entity concept")
        for metric in self.metrics:
            _known(f"metric {metric.name!r}", metric.requires, concepts)
            if metric.source not in {s.id for s in self.sources}:
                raise ValueError(f"metric {metric.name!r} cites unknown source {metric.source!r}")
        if self.scorable and not (self.lexicon or self.signatures):
            raise ValueError("a scorable pack needs a lexicon or signatures")
        if self.id in self.compatible_with:
            raise ValueError("a pack cannot list itself in compatible_with")
        return self


def _unique(what: str, names: list[str]) -> None:
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"duplicate {what} names: {dupes}")


def _known(where: str, names: list[str], concepts: dict[str, Concept]) -> None:
    missing = sorted(set(names) - set(concepts))
    if missing:
        raise ValueError(f"{where} references unknown concepts {missing}")
