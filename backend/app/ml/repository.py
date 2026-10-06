"""ML persistence and read-only data access (TASK-ML-009, TASK-DB-010).

- ``ModelRunRepository``: append-only ``model_runs`` store (no update/delete; migration 0011
  grants the app role INSERT/SELECT only).
- ``VersionReader``: loads the stored Parquet of one dataset version of the caller's workspace,
  read-only. Only versions that have reached INGESTED or later have a stored file.
"""

import polars as pl
from sqlalchemy import text

from app.core.errors import NotFoundError, SecurityError, StateConflictError
from app.core.scope import WorkspaceScope
from app.data.repository import DatasetRepository
from app.infra.db import UnitOfWork
from app.infra.repository import Repository
from app.infra.storage import Storage
from app.ml.domain import ModelRun


class ModelRunRepository(Repository):
    def insert(
        self,
        scope: WorkspaceScope,
        run: ModelRun,
        dataset_version_id: str,
        model_name: str,
        model_library: str,
    ) -> None:
        if run.workspace_id != scope.workspace_id:
            raise SecurityError("model run workspace does not match the caller's scope")
        self._session.execute(
            text(
                "INSERT INTO model_runs (id, workspace_id, dataset_version_id, task, model_name, "
                "model_library, seed, payload, evidence_id, created_at) VALUES (:id, :ws, :dsv, "
                ":task, :name, :lib, :seed, CAST(:payload AS jsonb), :ev, :at)"
            ),
            {
                "id": run.id,
                "ws": scope.workspace_id,
                "dsv": dataset_version_id,
                "task": run.task.value,
                "name": model_name,
                "lib": model_library,
                "seed": run.seed,
                "payload": run.model_dump_json(),
                "ev": run.evidence_id,
                "at": run.created_at,
            },
        )

    def get(self, scope: WorkspaceScope, run_id: str) -> ModelRun | None:
        row = self._session.execute(
            text(
                "SELECT payload, evidence_id FROM model_runs WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": run_id, "ws": scope.workspace_id},
        ).one_or_none()
        if row is None:
            return None
        run = ModelRun.model_validate(row.payload)
        return run.model_copy(update={"evidence_id": row.evidence_id or run.evidence_id})

    def model_ref(self, scope: WorkspaceScope, run_id: str) -> tuple[str, str] | None:
        """(model name, library@version) of a run in this workspace."""
        row = self._session.execute(
            text(
                "SELECT model_name, model_library FROM model_runs "
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": run_id, "ws": scope.workspace_id},
        ).one_or_none()
        return None if row is None else (row.model_name, row.model_library)

    def link_evidence(self, scope: WorkspaceScope, run_id: str, evidence_id: str) -> None:
        """Set the run's evidence once (migration 0015: UPDATE on this column only)."""
        self._session.execute(
            text(
                "UPDATE model_runs SET evidence_id = :ev WHERE id = :id AND workspace_id = :ws "
                "AND evidence_id IS NULL"
            ),
            {"ev": evidence_id, "id": run_id, "ws": scope.workspace_id},
        )


class VersionReader:
    def __init__(self, uow: UnitOfWork, storage: Storage) -> None:
        self._uow = uow
        self._storage = storage

    def read(self, scope: WorkspaceScope, version_id: str) -> pl.DataFrame:
        with self._uow(scope) as s:
            version = DatasetRepository(s).get_version(scope, version_id)
        if version is None:
            raise NotFoundError("dataset version not found")
        if version.storage_uri is None:
            raise StateConflictError(f"dataset version is {version.state}, not ingested")
        # ponytail: whole version in memory, like ProfilingService; scan_parquet if it outgrows RAM.
        with self._storage.open_read(version.storage_uri) as f:
            return pl.read_parquet(f)
