"""Understanding stages of the dataset pipeline (TASK-SCHEMA-010, TASK-DOMAIN-006, TASK-SEM-010,
TASK-INTEGRATION-002; design sections 9, 13-15).

PROFILED -> SCHEMA_ANALYZED -> DOMAIN_IDENTIFIED -> SEMANTICALLY_MAPPED. Each stage computes
outside a transaction, then commits its derived row (natural key, insert-only), the CAS state
transition, its review flags and an outbox event together. A re-run therefore resumes from the
last committed state and repeats nothing (design 9 "Retry"). A permanent failure moves the
version to ANALYSIS_FAILED with the reason; transient errors propagate so the job retries.

Semantic memory per dataset (TASK-SEM-010): a new version starts from its parent version's current
semantic model overrides (confirmed mappings + corrections) and a user domain override on the
parent is carried over, so a re-upload keeps what the user confirmed.
Single-table scope: every dataset version is one table; cross-table relationships across the
datasets of a workspace are not analysed yet.
"""

import re
from collections.abc import Callable, Sequence
from pathlib import PurePath

import polars as pl

from app.core.errors import ApplicationError, DataError, NotFoundError, StateConflictError
from app.core.ids import IdGenerator
from app.core.scope import WorkspaceScope
from app.data.domain import DatasetFailure, DatasetState, DatasetVersion
from app.data.profiling.domain import ProfileResult
from app.data.profiling.profiler import ENGINE_VERSION
from app.data.repository import DatasetRepository
from app.data.semantic.mapper import SemanticMapper
from app.data.semantic.overrides import remap
from app.data.understanding.analyzer import SchemaAnalyzer, TableInput
from app.data.understanding.domain import DomainClassification, DomainStatus, Understanding
from app.data.understanding.domain_detector import DomainDetector
from app.data.understanding_store import DomainSource, UnderstandingStore
from app.infra.db import UnitOfWork
from app.infra.storage import Storage

S = DatasetState
UNCERTAIN = frozenset({DomainStatus.AMBIGUOUS, DomainStatus.UNKNOWN})
_NON_TABLE_CHARS = re.compile(r"[^a-z0-9]+")


def table_name(dataset_name: str) -> str:
    """Stable table name for evidence and entity naming: 'Orders.csv' -> 'orders'."""
    return _NON_TABLE_CHARS.sub("_", PurePath(dataset_name).stem.lower()).strip("_") or "data"


def domain_flags(classification: DomainClassification) -> list[str]:
    return ["DOMAIN_UNCERTAIN"] if classification.status in UNCERTAIN else []


class _FrameCounter:
    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame

    def count_distinct(self, columns: Sequence[str]) -> int:
        return self._frame.select(list(columns)).unique().height


def understand_frame(
    analyzer: SchemaAnalyzer, table: str, profile: ProfileResult, frame: pl.DataFrame
) -> Understanding:
    """SchemaAnalyzer over one stored table, with value sets and composite-key counts."""

    def distinct(column: str) -> frozenset[str]:
        return frozenset(str(v) for v in frame[column].drop_nulls().unique().to_list())

    tables = [TableInput(table, profile, distinct=distinct, counter=_FrameCounter(frame))]
    return analyzer.analyze(tables)[table].understanding


class UnderstandingPipeline:
    def __init__(
        self,
        uow: UnitOfWork,
        storage: Storage,
        ids: IdGenerator,
        *,
        analyzer: SchemaAnalyzer | None = None,
        detector: DomainDetector | None = None,
        mapper: SemanticMapper | None = None,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._ids = ids
        self._analyzer = analyzer or SchemaAnalyzer()
        self._detector = detector or DomainDetector()
        self._mapper = mapper or SemanticMapper()

    def state(self, scope: WorkspaceScope, version_id: str) -> DatasetState:
        return self._version(scope, version_id).state

    def run(
        self, scope: WorkspaceScope, version_id: str, checkpoint: Callable[[], None] = lambda: None
    ) -> DatasetVersion:
        version = self._version(scope, version_id)
        stages = {
            S.PROFILED: self._schema,
            S.SCHEMA_ANALYZED: self._domain,
            S.DOMAIN_IDENTIFIED: self._semantic,
        }
        while version.state in stages:
            checkpoint()
            try:
                version = stages[version.state](scope, version)
            except StateConflictError:
                raise  # another worker advanced this version
            except (ApplicationError, ValueError) as exc:
                if isinstance(exc, ApplicationError) and exc.retryable:
                    raise
                self._fail(scope, version, exc)
                raise DataError(
                    "Data understanding failed for this dataset version.", retryable=False
                ) from exc
        return version

    def _schema(self, scope: WorkspaceScope, version: DatasetVersion) -> DatasetVersion:
        with self._uow(scope) as s:
            repo = DatasetRepository(s)
            raw_profile = repo.get_profile(scope, version.id, ENGINE_VERSION)
            dataset = repo.get_dataset(scope, version.dataset_id)
            existing = UnderstandingStore(s).get(scope, version.id, self._analyzer.engine_version)
        if raw_profile is None or dataset is None or version.storage_uri is None:
            raise NotFoundError("profile or stored data missing for a PROFILED version")
        understanding = existing or self._analyze(
            table_name(dataset.name), ProfileResult.model_validate(raw_profile), version.storage_uri
        )
        with self._uow(scope) as s:
            store = UnderstandingStore(s)
            store.save(scope, understanding)
            saved = store.get(scope, version.id, understanding.engine_version) or understanding
            return self._advance(
                DatasetRepository(s),
                scope,
                version,
                S.SCHEMA_ANALYZED,
                saved.review_flags,
                "DatasetSchemaAnalyzed",
            )

    def _analyze(self, table: str, profile: ProfileResult, storage_uri: str) -> Understanding:
        # ponytail: whole version in memory (as the profiler does); scan + per-column distinct
        # sets if versions outgrow RAM.
        with self._storage.open_read(storage_uri) as f:
            frame = pl.read_parquet(f)
        return understand_frame(self._analyzer, table, profile, frame)

    def _domain(self, scope: WorkspaceScope, version: DatasetVersion) -> DatasetVersion:
        with self._uow(scope) as s:
            store = UnderstandingStore(s)
            understanding = store.latest(scope, version.id)
            existing = store.domain(scope, version.id)
            parent = store.domain(scope, version.parent_id) if version.parent_id else None
        if understanding is None:
            raise NotFoundError("understanding missing for a SCHEMA_ANALYZED version")
        source: DomainSource = "detector"
        if existing is not None:
            classification, source = existing.classification, existing.source
        elif parent is not None and parent.source == "user_override":
            classification, source = parent.classification, "user_override"
        else:
            classification = self._detector.detect([understanding])
        with self._uow(scope) as s:
            record = UnderstandingStore(s).put_domain(
                scope,
                version.id,
                classification,
                source,
                created_by=scope.principal_id,
                append=False,
            )
            return self._advance(
                DatasetRepository(s),
                scope,
                version,
                S.DOMAIN_IDENTIFIED,
                domain_flags(record.classification),
                "DatasetDomainIdentified",
            )

    def _semantic(self, scope: WorkspaceScope, version: DatasetVersion) -> DatasetVersion:
        with self._uow(scope) as s:
            store = UnderstandingStore(s)
            understanding = store.latest(scope, version.id)
            domain = store.domain(scope, version.id)
            parent_model = (
                store.semantic_model(scope, version.parent_id) if version.parent_id else None
            )
            dataset = DatasetRepository(s).get_dataset(scope, version.dataset_id)
        if understanding is None or domain is None or dataset is None:
            raise NotFoundError("understanding or domain missing for a DOMAIN_IDENTIFIED version")
        remapped = remap(
            self._mapper,
            understanding,
            domain.classification,
            table=table_name(dataset.name),
            overrides=parent_model.overrides if parent_model else [],
        )
        with self._uow(scope) as s:
            store = UnderstandingStore(s)
            if store.semantic_model(scope, version.id) is None:
                store.put_semantic_model(scope, remapped.model, "active")
            model = store.semantic_model(scope, version.id) or remapped.model
            return self._advance(
                DatasetRepository(s),
                scope,
                version,
                S.SEMANTICALLY_MAPPED,
                remapped.review_flags,
                "SemanticModelChanged",
                semantic_model_version=model.version,
            )

    def _advance(
        self,
        repo: DatasetRepository,
        scope: WorkspaceScope,
        version: DatasetVersion,
        to: DatasetState,
        flags: Sequence[str],
        event_type: str,
        **extra: str | int,
    ) -> DatasetVersion:
        done = repo.transition(scope, version.id, version.state, to, add_flags=flags)
        event = {"dataset_id": version.dataset_id, "version_id": version.id, **extra}
        repo.add_outbox(scope, self._ids.new("evt"), event_type, event)
        return done

    def _fail(self, scope: WorkspaceScope, version: DatasetVersion, exc: Exception) -> None:
        code = exc.code if isinstance(exc, ApplicationError) else "UNDERSTANDING_FAILED"
        message = exc.message if isinstance(exc, ApplicationError) else "invalid derived data"
        failure = DatasetFailure(code=code, message=message)
        with self._uow(scope) as s:
            DatasetRepository(s).transition(
                scope, version.id, version.state, S.ANALYSIS_FAILED, failure=failure
            )

    def _version(self, scope: WorkspaceScope, version_id: str) -> DatasetVersion:
        with self._uow(scope) as s:
            version = DatasetRepository(s).get_version(scope, version_id)
        if version is None:
            raise NotFoundError("dataset version not found")
        return version
