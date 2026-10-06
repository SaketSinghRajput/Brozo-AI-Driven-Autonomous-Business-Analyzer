"""Dataset / DatasetVersion / profile persistence (TASK-DATA-009, design sections 9, 50-52).

State changes happen only through `transition`, a compare-and-set
`UPDATE ... WHERE state = :from` that also validates the edge against the section 9 diagram.
An illegal edge or a lost race raises `StateConflictError`. Review flags are merged (set union,
sorted) in the same statement; `failure` is set exactly on *_FAILED states, cleared otherwise.
Profiles are insert-only, keyed by (dataset_version_id, engine_version): a rerun is a no-op.
"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import Row, text

from app.core.errors import StateConflictError
from app.core.scope import WorkspaceScope
from app.data.domain import Dataset, DatasetFailure, DatasetState, DatasetVersion, can_transition
from app.infra.repository import Repository

# Columns a transition may also set (module constants; never caller-supplied names).
_FINALIZE_COLUMNS = ("content_hash", "storage_uri", "row_count", "ingest_report")
_VERSION_COLUMNS = (
    "id, dataset_id, workspace_id, version_no, parent_version_id AS parent_id, source_sha256, "
    "content_hash, storage_uri, row_count, state, state_changed_at, failure, review_flags, "
    "created_at"
)
_DATASET_COLUMNS = "id, workspace_id, name, source_type, created_at"


def _version(row: Row[Any]) -> DatasetVersion:
    return DatasetVersion.model_validate(dict(row._mapping))


def _dataset(row: Row[Any]) -> Dataset:
    return Dataset.model_validate(dict(row._mapping))


class DatasetRepository(Repository):
    def create_dataset(
        self, scope: WorkspaceScope, dataset_id: str, name: str, source_type: str
    ) -> Dataset:
        row = self._session.execute(
            text(
                "INSERT INTO datasets (id, workspace_id, name, source_type) "  # noqa: S608
                f"VALUES (:id, :ws, :name, :type) RETURNING {_DATASET_COLUMNS}"
            ),
            {"id": dataset_id, "ws": scope.workspace_id, "name": name, "type": source_type},
        ).one()
        return _dataset(row)

    def get_dataset(self, scope: WorkspaceScope, dataset_id: str) -> Dataset | None:
        row = self._session.execute(
            text(
                f"SELECT {_DATASET_COLUMNS} FROM datasets "  # noqa: S608
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": dataset_id, "ws": scope.workspace_id},
        ).one_or_none()
        return None if row is None else _dataset(row)

    def list_datasets(self, scope: WorkspaceScope, limit: int, offset: int) -> list[Dataset]:
        rows = self._session.execute(
            text(
                f"SELECT {_DATASET_COLUMNS} FROM datasets WHERE workspace_id = :ws "  # noqa: S608
                "ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
            ),
            {"ws": scope.workspace_id, "limit": limit, "offset": offset},
        ).all()
        return [_dataset(r) for r in rows]

    def create_version(
        self, scope: WorkspaceScope, version_id: str, dataset_id: str, source_sha256: str
    ) -> DatasetVersion:
        """Next version_no of the dataset; the latest version becomes the parent."""
        row = self._session.execute(
            text(
                "WITH latest AS (SELECT id, version_no FROM dataset_versions "  # noqa: S608
                "WHERE dataset_id = :ds AND workspace_id = :ws "
                "ORDER BY version_no DESC LIMIT 1) "
                "INSERT INTO dataset_versions (id, workspace_id, dataset_id, version_no, "
                "parent_version_id, source_sha256, state) "
                "SELECT :id, :ws, :ds, COALESCE((SELECT version_no FROM latest), 0) + 1, "
                "(SELECT id FROM latest), :sha, 'UPLOADED' "
                f"RETURNING {_VERSION_COLUMNS}"
            ),
            {"id": version_id, "ws": scope.workspace_id, "ds": dataset_id, "sha": source_sha256},
        ).one()
        return _version(row)

    def get_version(self, scope: WorkspaceScope, version_id: str) -> DatasetVersion | None:
        row = self._session.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM dataset_versions "  # noqa: S608
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": version_id, "ws": scope.workspace_id},
        ).one_or_none()
        return None if row is None else _version(row)

    def find_by_source(self, scope: WorkspaceScope, source_sha256: str) -> DatasetVersion | None:
        row = self._session.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM dataset_versions "  # noqa: S608
                "WHERE workspace_id = :ws AND source_sha256 = :sha"
            ),
            {"ws": scope.workspace_id, "sha": source_sha256},
        ).one_or_none()
        return None if row is None else _version(row)

    def list_versions(self, scope: WorkspaceScope, dataset_id: str) -> list[DatasetVersion]:
        rows = self._session.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM dataset_versions "  # noqa: S608
                "WHERE dataset_id = :ds AND workspace_id = :ws ORDER BY version_no"
            ),
            {"ds": dataset_id, "ws": scope.workspace_id},
        ).all()
        return [_version(r) for r in rows]

    def ingest_report(self, scope: WorkspaceScope, version_id: str) -> dict[str, Any] | None:
        report = self._session.execute(
            text(
                "SELECT ingest_report FROM dataset_versions WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": version_id, "ws": scope.workspace_id},
        ).scalar_one_or_none()
        return None if report is None else dict(report)

    def ingest_job_id(self, scope: WorkspaceScope, version_id: str) -> str | None:
        job_id = self._session.execute(
            text(
                "SELECT id FROM jobs WHERE workspace_id = :ws AND type = 'ingest' "
                "AND payload->>'version_id' = :v ORDER BY created_at LIMIT 1"
            ),
            {"ws": scope.workspace_id, "v": version_id},
        ).scalar_one_or_none()
        return None if job_id is None else str(job_id)

    def job_for_key(
        self, scope: WorkspaceScope, idempotency_key: str
    ) -> tuple[str, dict[str, Any]] | None:
        """The ingest job created by an earlier upload with this Idempotency-Key, if any."""
        row = self._session.execute(
            text(
                "SELECT id, payload FROM jobs WHERE workspace_id = :ws AND type = 'ingest' "
                "AND idempotency_key = :key"
            ),
            {"ws": scope.workspace_id, "key": idempotency_key},
        ).one_or_none()
        return None if row is None else (str(row.id), dict(row.payload))

    def transition(
        self,
        scope: WorkspaceScope,
        version_id: str,
        from_: DatasetState,
        to: DatasetState,
        *,
        failure: DatasetFailure | None = None,
        add_flags: Iterable[str] = (),
        finalize: Mapping[str, Any] | None = None,
    ) -> DatasetVersion:
        if not can_transition(from_, to):
            raise StateConflictError(f"illegal dataset state transition {from_} -> {to}")
        extra = dict(finalize or {})
        if set(extra) - set(_FINALIZE_COLUMNS):
            raise ValueError("unknown finalize column")
        if "ingest_report" in extra:
            extra["ingest_report"] = json.dumps(extra["ingest_report"])
        sets = "".join(
            f", {c} = CAST(:{c} AS jsonb)" if c == "ingest_report" else f", {c} = :{c}"
            for c in extra
        )
        row = self._session.execute(
            text(
                "UPDATE dataset_versions SET state = :to, state_changed_at = now(), "  # noqa: S608
                "failure = CAST(:failure AS jsonb), review_flags = (SELECT COALESCE("
                "jsonb_agg(DISTINCT f ORDER BY f), '[]'::jsonb) FROM jsonb_array_elements_text("
                f"review_flags || CAST(:flags AS jsonb)) AS f){sets} "
                "WHERE id = :id AND workspace_id = :ws AND state = :from "
                f"RETURNING {_VERSION_COLUMNS}"
            ),
            {
                "id": version_id,
                "ws": scope.workspace_id,
                "from": from_.value,
                "to": to.value,
                "failure": None if failure is None else failure.model_dump_json(),
                "flags": json.dumps(sorted(set(add_flags))),
                **extra,
            },
        ).one_or_none()
        if row is None:
            raise StateConflictError(f"dataset version is no longer in state {from_}")
        return _version(row)

    def add_outbox(
        self, scope: WorkspaceScope, event_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        self._session.execute(
            text(
                "INSERT INTO outbox_events (id, event_type, version, payload) "
                "VALUES (:id, :type, 1, CAST(:payload AS jsonb))"
            ),
            {
                "id": event_id,
                "type": event_type,
                "payload": json.dumps({"workspace_id": scope.workspace_id, **payload}),
            },
        )

    def put_profile(
        self, scope: WorkspaceScope, version_id: str, engine_version: str, profile: str
    ) -> bool:
        """Insert-only upsert by natural key; returns False when it already existed."""
        inserted = self._session.execute(
            text(
                "INSERT INTO dataset_profiles (dataset_version_id, workspace_id, engine_version, "
                "profile) VALUES (:v, :ws, :engine, CAST(:profile AS jsonb)) "
                "ON CONFLICT (dataset_version_id, engine_version) DO NOTHING "
                "RETURNING dataset_version_id"
            ),
            {
                "v": version_id,
                "ws": scope.workspace_id,
                "engine": engine_version,
                "profile": profile,
            },
        ).scalar_one_or_none()
        return inserted is not None

    def get_profile(
        self, scope: WorkspaceScope, version_id: str, engine_version: str
    ) -> dict[str, Any] | None:
        profile = self._session.execute(
            text(
                "SELECT profile FROM dataset_profiles WHERE dataset_version_id = :v "
                "AND workspace_id = :ws AND engine_version = :engine"
            ),
            {"v": version_id, "ws": scope.workspace_id, "engine": engine_version},
        ).scalar_one_or_none()
        return None if profile is None else dict(profile)
