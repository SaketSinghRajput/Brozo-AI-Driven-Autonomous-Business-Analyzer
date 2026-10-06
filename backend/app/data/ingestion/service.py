"""IngestionService (TASK-DATA-011, design.md section 10 algorithm).

UPLOADED → VALIDATING → (VALIDATION_FAILED | INGESTING) → (INGESTION_FAILED | INGESTED).
Idempotent per the worker contract: a version already past INGESTING (or terminally failed) is
a no-op; a re-run after a crash resumes from VALIDATING/INGESTING (the lease guarantees one
runner). The stored Parquet only becomes visible with the finalize transaction; a leftover file
from a crashed attempt was never referenced and is replaced.
Dedupe by `source_sha256` (ADR-0023) happens at upload and is enforced by a unique index, so a
version reaching this service is never a byte-duplicate of another in the workspace.
"""

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ApplicationError, DataError, NotFoundError, SecurityError
from app.core.ids import IdGenerator
from app.core.scope import WorkspaceScope
from app.data.domain import DatasetFailure, DatasetState, DatasetVersion, ParseLimits
from app.data.ingestion.errors import reason_of
from app.data.ingestion.inspector import FileInspector, InspectionLimits
from app.data.ingestion.pipeline import PipelineLimits, ingest_file, parser_for
from app.data.ingestion.writer import content_hash
from app.data.repository import DatasetRepository
from app.infra.db import UnitOfWork
from app.infra.storage import Storage, StorageError
from app.jobs.models import JobType
from app.jobs.repository import JobRepository

S = DatasetState
_PENDING = frozenset({S.UPLOADED, S.VALIDATING, S.INGESTING})


def raw_key(scope: WorkspaceScope, version_id: str) -> str:
    return f"raw/{scope.workspace_id}/{version_id}"


def parquet_key(scope: WorkspaceScope, version: DatasetVersion) -> str:
    return f"datasets/{scope.workspace_id}/{version.dataset_id}/{version.id}.parquet"


@dataclass(frozen=True)
class IngestionSettings:
    inspection: InspectionLimits
    pipeline: PipelineLimits

    @classmethod
    def for_upload_limit(cls, max_bytes: int) -> IngestionSettings:
        # ponytail: row/reject limits are constants until BENCH/PRD OPEN-03 sets ceilings;
        # move them into StorageConfig when they need tuning per deployment.
        return cls(
            inspection=InspectionLimits(max_bytes=max_bytes),
            pipeline=PipelineLimits(
                ParseLimits(max_rows=10_000_000, max_reject_ratio=0.25, max_field_chars=100_000)
            ),
        )


class IngestionService:
    def __init__(
        self, uow: UnitOfWork, storage: Storage, ids: IdGenerator, settings: IngestionSettings
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._ids = ids
        self._settings = settings

    def ingest(
        self,
        scope: WorkspaceScope,
        version_id: str,
        *,
        filename: str | None = None,
        sheet: str | None = None,
        on_batch: Callable[[int], None] = lambda _: None,
    ) -> DatasetVersion:
        version = self._get(scope, version_id)
        if version.state not in _PENDING:
            return version
        if version.state is S.UPLOADED:
            version = self._move(scope, version, S.VALIDATING)
        with tempfile.TemporaryDirectory(prefix="ingest-") as tmp:
            workdir = Path(tmp)
            source = workdir / "source"
            with self._storage.open_read(raw_key(scope, version.id)) as src, source.open("wb") as f:
                shutil.copyfileobj(src, f)
            try:
                facts = FileInspector(self._settings.inspection).inspect(source)
                parser = parser_for(facts, sheet, self._settings.pipeline)
            except (SecurityError, DataError) as exc:
                # A resumed INGESTING version already passed validation once; same bytes, same
                # verdict, so this branch only fires from VALIDATING in practice.
                to = S.VALIDATION_FAILED if version.state is S.VALIDATING else S.INGESTION_FAILED
                self.fail(scope, version.id, version.state, to, exc)
                raise
            if version.state is S.VALIDATING:
                version = self._move(scope, version, S.INGESTING)
            key = parquet_key(scope, version)
            if self._storage.exists(key):
                self._storage.delete(key)
            try:
                report = ingest_file(
                    facts,
                    parser,
                    filename=filename,
                    limits=self._settings.pipeline,
                    storage=self._storage,
                    key=key,
                    workdir=workdir,
                    on_batch=on_batch,
                )
            except (DataError, SecurityError, StorageError) as exc:
                self.fail(scope, version.id, S.INGESTING, S.INGESTION_FAILED, exc)
                raise
        digest = content_hash(self._storage, key)
        with self._uow(scope) as s:
            repo = DatasetRepository(s)
            done = repo.transition(
                scope,
                version.id,
                S.INGESTING,
                S.INGESTED,
                add_flags=report.flags,
                finalize={
                    "content_hash": digest,
                    "storage_uri": key,
                    "row_count": report.rows_ingested,
                    "ingest_report": report.model_dump(mode="json"),
                },
            )
            event = {"dataset_id": done.dataset_id, "version_id": done.id}
            repo.add_outbox(scope, self._ids.new("evt"), "DatasetIngested", event)
            JobRepository(s).enqueue(
                scope,
                self._ids.new("job"),
                JobType.PROCESS_DATASET,
                event,
                idempotency_key=f"process:{done.id}",
            )
        return done

    def fail(
        self,
        scope: WorkspaceScope,
        version_id: str,
        from_: DatasetState,
        to: DatasetState,
        exc: ApplicationError,
    ) -> None:
        failure = DatasetFailure(code=reason_of(exc), message=exc.message)
        with self._uow(scope) as s:
            DatasetRepository(s).transition(scope, version_id, from_, to, failure=failure)

    def _get(self, scope: WorkspaceScope, version_id: str) -> DatasetVersion:
        with self._uow(scope) as s:
            version = DatasetRepository(s).get_version(scope, version_id)
        if version is None:
            raise NotFoundError("dataset version not found")
        return version

    def _move(
        self, scope: WorkspaceScope, version: DatasetVersion, to: DatasetState
    ) -> DatasetVersion:
        with self._uow(scope) as s:
            return DatasetRepository(s).transition(scope, version.id, version.state, to)
