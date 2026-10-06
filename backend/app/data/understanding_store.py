"""Persistence of understanding, domain classification and semantic models (TASK-SCHEMA-010,
TASK-DOMAIN-006, TASK-SEM-009; design sections 9, 13-15, 74).

All three are insert-only and keyed by natural keys, so a re-run is a no-op:
- ``understandings``: (dataset_version_id, engine_version); this class is the SQL adapter of the
  ``UnderstandingRepository`` port (app.data.understanding.analyzer);
- ``domain_classifications``: (dataset_version_id, seq); seq 1 = the pipeline's result, every
  user override appends max + 1, and the highest seq is current;
- ``semantic_models``: (dataset_version_id, version); the highest version is current.
``created_by`` of semantic models stays NULL (it references users.id, and system actors are not
users); the author of every correction is in ``SemanticModel.overrides[].by`` and the audit log.
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from app.core.scope import WorkspaceScope
from app.data.semantic.domain import SemanticModel
from app.data.understanding.domain import DomainClassification, Understanding
from app.infra.repository import Repository

DomainSource = Literal["detector", "user_override"]
ModelStatus = Literal["active", "overridden"]


@dataclass(frozen=True)
class DomainRecord:
    seq: int
    source: DomainSource
    classification: DomainClassification


class UnderstandingStore(Repository):
    def get(
        self, scope: WorkspaceScope, dataset_version_id: str, engine_version: str
    ) -> Understanding | None:
        raw = self._session.execute(
            text(
                "SELECT understanding FROM understandings WHERE workspace_id = :ws "
                "AND dataset_version_id = :v AND engine_version = :engine"
            ),
            {"ws": scope.workspace_id, "v": dataset_version_id, "engine": engine_version},
        ).scalar_one_or_none()
        return None if raw is None else Understanding.model_validate(raw)

    def save(self, scope: WorkspaceScope, understanding: Understanding) -> None:
        self._session.execute(
            text(
                "INSERT INTO understandings (dataset_version_id, workspace_id, engine_version, "
                "understanding) VALUES (:v, :ws, :engine, CAST(:u AS jsonb)) "
                "ON CONFLICT (dataset_version_id, engine_version) DO NOTHING"
            ),
            {
                "v": understanding.dataset_version_id,
                "ws": scope.workspace_id,
                "engine": understanding.engine_version,
                "u": understanding.model_dump_json(by_alias=True),
            },
        )

    def latest(self, scope: WorkspaceScope, dataset_version_id: str) -> Understanding | None:
        raw = self._session.execute(
            text(
                "SELECT understanding FROM understandings WHERE workspace_id = :ws "
                "AND dataset_version_id = :v ORDER BY created_at DESC, engine_version DESC LIMIT 1"
            ),
            {"ws": scope.workspace_id, "v": dataset_version_id},
        ).scalar_one_or_none()
        return None if raw is None else Understanding.model_validate(raw)

    def put_domain(
        self,
        scope: WorkspaceScope,
        dataset_version_id: str,
        classification: DomainClassification,
        source: DomainSource,
        *,
        created_by: str,
        append: bool,
    ) -> DomainRecord:
        """``append=False``: the pipeline's seq-1 row (idempotent); True: next seq (override)."""
        seq = (
            "(SELECT COALESCE(MAX(seq), 0) + 1 FROM domain_classifications "
            "WHERE dataset_version_id = :v)"
            if append
            else "1"
        )
        self._session.execute(
            text(
                "INSERT INTO domain_classifications (dataset_version_id, workspace_id, seq, "  # noqa: S608
                f"classification, source, created_by) VALUES (:v, :ws, {seq}, "
                "CAST(:c AS jsonb), :source, :by) ON CONFLICT (dataset_version_id, seq) DO NOTHING"
            ),
            {
                "v": dataset_version_id,
                "ws": scope.workspace_id,
                "c": classification.model_dump_json(),
                "source": source,
                "by": created_by,
            },
        )
        record = self.domain(scope, dataset_version_id)
        if record is None:  # pragma: no cover - the row was written in this transaction
            raise RuntimeError("domain classification row missing after insert")
        return record

    def domain(self, scope: WorkspaceScope, dataset_version_id: str) -> DomainRecord | None:
        row = self._session.execute(
            text(
                "SELECT seq, source, classification FROM domain_classifications "
                "WHERE workspace_id = :ws AND dataset_version_id = :v ORDER BY seq DESC LIMIT 1"
            ),
            {"ws": scope.workspace_id, "v": dataset_version_id},
        ).one_or_none()
        if row is None:
            return None
        return DomainRecord(
            row.seq, row.source, DomainClassification.model_validate(row.classification)
        )

    def put_semantic_model(
        self, scope: WorkspaceScope, model: SemanticModel, status: ModelStatus
    ) -> bool:
        """False when that model version already exists (a replay, or a lost race)."""
        inserted = self._session.execute(
            text(
                "INSERT INTO semantic_models (id, workspace_id, dataset_version_id, version, "
                "model, status) VALUES (:id, :ws, :v, :n, CAST(:m AS jsonb), :status) "
                "ON CONFLICT DO NOTHING RETURNING id"
            ),
            {
                "id": model.semantic_model_id,
                "ws": scope.workspace_id,
                "v": model.dataset_version_id,
                "n": model.version,
                "m": model.model_dump_json(by_alias=True),
                "status": status,
            },
        ).scalar_one_or_none()
        return inserted is not None

    def semantic_model(
        self, scope: WorkspaceScope, dataset_version_id: str, version: int | None = None
    ) -> SemanticModel | None:
        """The given model version, or the current (highest) one."""
        raw = self._session.execute(
            text(
                "SELECT model FROM semantic_models WHERE workspace_id = :ws "
                "AND dataset_version_id = :v AND (CAST(:n AS integer) IS NULL OR version = :n) "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"ws": scope.workspace_id, "v": dataset_version_id, "n": version},
        ).scalar_one_or_none()
        return None if raw is None else SemanticModel.model_validate(raw)
