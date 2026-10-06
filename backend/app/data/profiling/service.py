"""ProfilingService (TASK-PROFILE-009): profile a stored version, persist, INGESTED → PROFILED.

The profile row (natural key `(dataset_version_id, engine_version)`), the state transition, the
review flags and the DatasetProfiled outbox event are written in one transaction. A rerun on an
already profiled version is a no-op (the job may run twice, per the worker contract).
"""

from typing import Any

import polars as pl

from app.core.errors import NotFoundError, StateConflictError
from app.core.ids import IdGenerator
from app.core.scope import WorkspaceScope
from app.data.domain import DatasetState
from app.data.profiling.domain import ProfileResult
from app.data.profiling.numeric import near_duplicate_flags
from app.data.profiling.profiler import ENGINE_VERSION, IngestColumn, profile_frame
from app.data.repository import DatasetRepository
from app.infra.db import UnitOfWork
from app.infra.storage import Storage

S = DatasetState
_BEFORE_STORED = frozenset(
    {S.UPLOADED, S.VALIDATING, S.VALIDATION_FAILED, S.INGESTING, S.INGESTION_FAILED}
)


def profile_flags(profile: ProfileResult) -> list[str]:
    flags = near_duplicate_flags(profile.correlations)
    if profile.duplicate_rows:
        flags.append("DUPLICATE_ROWS")
    flags += [f"ALL_NULL_COLUMN:{c.name}" for c in profile.columns if c.null_ratio == 1.0]
    return flags


def ingest_columns(report: dict[str, Any]) -> dict[str, IngestColumn]:
    return {
        str(c["name"]): IngestColumn(
            physical_type=c["physical_type"],
            parse_success_ratio=float(c["parse_success_ratio"]),
            tz_mixed=bool(c.get("tz_mixed")),
            unit_hint=c.get("unit_hint"),
        )
        for c in report.get("columns") or []
    }


class ProfilingService:
    def __init__(
        self, uow: UnitOfWork, storage: Storage, ids: IdGenerator, *, sample_rows: int = 100_000
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._ids = ids
        self._sample_rows = sample_rows

    def profile(self, scope: WorkspaceScope, version_id: str) -> ProfileResult:
        with self._uow(scope) as s:
            repo = DatasetRepository(s)
            version = repo.get_version(scope, version_id)
            if version is None:
                raise NotFoundError("dataset version not found")
            existing = repo.get_profile(scope, version_id, ENGINE_VERSION)
            report = repo.ingest_report(scope, version_id) or {}
        if version.state in _BEFORE_STORED or version.storage_uri is None:
            raise StateConflictError(f"dataset version is {version.state}, not ingested")
        if existing is not None and version.state is not S.INGESTED:
            return ProfileResult.model_validate(existing)
        # ponytail: reads the whole version into memory (columnar, typed). Switch to
        # scan_parquet + streaming aggregations if versions outgrow RAM (PRD OPEN-03).
        with self._storage.open_read(version.storage_uri) as f:
            frame = pl.read_parquet(f)
        profile = profile_frame(
            frame, version.id, ingest_columns(report), sample_rows=self._sample_rows
        )
        with self._uow(scope) as s:
            repo = DatasetRepository(s)
            repo.put_profile(scope, version_id, ENGINE_VERSION, profile.model_dump_json())
            if version.state is S.INGESTED:
                repo.transition(
                    scope, version_id, S.INGESTED, S.PROFILED, add_flags=profile_flags(profile)
                )
                event = {"dataset_id": version.dataset_id, "version_id": version_id}
                repo.add_outbox(scope, self._ids.new("evt"), "DatasetProfiled", event)
        return profile
