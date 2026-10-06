"""SchemaAnalyzer (TASK-SCHEMA-010; design 13): ProfileResult(s) -> Understanding per table.

Pure orchestration of the detectors; no I/O. Relationships need values across tables, so the
analyzer takes every table of a workspace batch at once (``distinct`` callables supply the value
sets). Persistence is behind ``UnderstandingRepository`` (rows of the ``understandings`` table,
keyed by dataset_version_id + engine_version); ``UnderstandingService`` makes a run idempotent.
The SQL adapter is ``app.data.understanding_store.UnderstandingStore``; the SCHEMA_ANALYZED
transition runs in ``app.data.pipeline`` and ``GET .../understanding`` in
``app.data.understanding_api``.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.core.scope import WorkspaceScope
from app.data.profiling.domain import ProfileResult
from app.data.understanding.config import UnderstandingConfig, default_config
from app.data.understanding.dimensions import DimensionDetector
from app.data.understanding.domain import Understanding
from app.data.understanding.entities import DistinctCounter, EntityDetector
from app.data.understanding.measures import MetricDetector
from app.data.understanding.relationships import RelationshipDetector, TableSnapshot
from app.data.understanding.scoring import Decision
from app.data.understanding.semantic_types import SemanticTypeClassifier
from app.data.understanding.time_dimensions import TimeDimensionDetector

ENGINE = "schema-analyzer@1"


@dataclass(frozen=True)
class TableInput:
    name: str
    profile: ProfileResult
    distinct: Callable[[str], frozenset[str]] | None = None  # needed for relationships
    counter: DistinctCounter | None = None  # needed for composite keys


@dataclass(frozen=True)
class TableAnalysis:
    understanding: Understanding
    type_decisions: dict[str, Decision]
    relationship_evidence: dict[str, list[str]] = field(default_factory=dict)  # "from->to"


class UnderstandingRepository(Protocol):
    def get(
        self, scope: WorkspaceScope, dataset_version_id: str, engine_version: str
    ) -> Understanding | None: ...

    def save(self, scope: WorkspaceScope, understanding: Understanding) -> None: ...


class SchemaAnalyzer:
    def __init__(
        self,
        config: UnderstandingConfig | None = None,
        classifier: SemanticTypeClassifier | None = None,
    ) -> None:
        self._cfg = config or default_config()
        self._classifier = classifier or SemanticTypeClassifier(self._cfg)
        self._entities = EntityDetector(self._cfg)
        self._relationships = RelationshipDetector(self._cfg)
        self._time = TimeDimensionDetector(self._cfg)
        self._measures = MetricDetector()
        self._dimensions = DimensionDetector(self._cfg)

    @property
    def engine_version(self) -> str:
        return f"{ENGINE}+{self._cfg.version}+{self._classifier.version}"

    def analyze(
        self, tables: Sequence[TableInput], default_currency: str | None = None
    ) -> dict[str, TableAnalysis]:
        names = [t.name for t in tables]
        if len(names) != len(set(names)):
            raise ValueError("table names must be unique within one analysis")
        typed = {t.name: self._classifier.classify(t.profile) for t in tables}
        snapshots = [
            TableSnapshot(t.name, t.profile, typed[t.name].columns, t.distinct)
            for t in tables
            if t.distinct is not None
        ]
        results = {}
        for table in tables:
            typing = typed[table.name]
            columns = typing.columns
            entities = self._entities.detect(table.name, table.profile, columns, table.counter)
            by_profile = {c.name: c for c in table.profile.columns}
            keys = frozenset(
                e.key_columns[0]
                for e in entities
                if len(e.key_columns) == 1 and self._entities.is_key(by_profile[e.key_columns[0]])
            )
            composite = [e for e in entities if len(e.key_columns) > 1]
            source = next((s for s in snapshots if s.name == table.name), None)
            findings = self._relationships.detect(source, snapshots) if source else []
            measures = self._measures.detect(table.profile, columns, keys, default_currency)
            flags = list(typing.review_flags)
            if not keys and not composite:
                flags.append("NO_PRIMARY_KEY")
            flags += [f"CURRENCY_UNKNOWN:{m.column}" for m in measures if m.unit == "currency"]
            understanding = Understanding(
                dataset_version_id=table.profile.dataset_version_id,
                engine_version=self.engine_version,
                columns=columns,
                entities=entities,
                relationships=[f.relationship for f in findings],
                time_dimensions=self._time.detect(table.profile, columns),
                measures=measures,
                dimensions=self._dimensions.detect(table.profile, columns, keys),
                review_flags=flags,
            )
            evidence = {
                f"{f.relationship.from_}->{f.relationship.to}": f.evidence for f in findings
            }
            results[table.name] = TableAnalysis(understanding, typing.decisions, evidence)
        return results


class UnderstandingService:
    """Idempotent: an existing Understanding for (version, engine) is returned, not recomputed."""

    def __init__(self, analyzer: SchemaAnalyzer, repository: UnderstandingRepository) -> None:
        self._analyzer = analyzer
        self._repository = repository

    def understand(
        self,
        scope: WorkspaceScope,
        table: TableInput,
        related: Sequence[TableInput] = (),
        default_currency: str | None = None,
    ) -> Understanding:
        version_id = table.profile.dataset_version_id
        existing = self._repository.get(scope, version_id, self._analyzer.engine_version)
        if existing is not None:
            return existing
        analysis = self._analyzer.analyze([table, *related], default_currency)
        understanding = analysis[table.name].understanding
        self._repository.save(scope, understanding)
        return understanding
