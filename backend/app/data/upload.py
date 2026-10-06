"""Upload registration (TASK-DATA-010, design sections 51, 54, 74).

Streams the upload into the store (`raw/<ws>/<dsv>`, key from generated ids, never the user's
filename) while hashing and enforcing the size cap, then registers dataset (if new) + version
(UPLOADED) + ingest job + DatasetUploaded outbox event in one unit of work.

Idempotency: an `Idempotency-Key` replay returns the original ids (via `jobs.idempotency_key`);
identical bytes in the workspace return the existing version (`source_sha256`, ADR-0023,
unique per workspace so concurrent duplicates also resolve to one version).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, BinaryIO

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from app.core.errors import DataError, NotFoundError
from app.core.ids import IdGenerator
from app.core.scope import WorkspaceScope
from app.data.domain import DatasetState, DatasetVersion
from app.data.ingestion.errors import UnsupportedFormatError, UploadTooLargeError, reject
from app.data.ingestion.inspector import sniff_head
from app.data.ingestion.service import raw_key
from app.data.repository import DatasetRepository
from app.infra.db import UnitOfWork
from app.infra.storage import Storage
from app.jobs.models import JobType
from app.jobs.repository import JobRepository

_CHUNK = 1 << 20
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


class UploadResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    version_id: str
    job_id: str
    status: DatasetState


@dataclass(frozen=True)
class UploadRequest:
    filename: str | None
    name: str | None = None
    dataset_id: str | None = None
    sheet: str | None = None
    idempotency_key: str | None = None


class _Existing(Exception):
    """Duplicate bytes or a replayed key: roll back our inserts and answer with the original."""

    def __init__(self, response: UploadResponse) -> None:
        self.response = response


def safe_filename(filename: str | None) -> str | None:
    """Display-only basename: no directories (either separator), no control characters."""
    if not filename:
        return None
    base = _UNSAFE.sub("", PureWindowsPath(filename.replace("/", "\\")).name).strip()
    return base[:255] if base.strip(".") else None


def _describe(repo: DatasetRepository, scope: WorkspaceScope, v: DatasetVersion) -> UploadResponse:
    return UploadResponse(
        dataset_id=v.dataset_id,
        version_id=v.id,
        job_id=repo.ingest_job_id(scope, v.id) or "",
        status=v.state,
    )


class UploadService:
    def __init__(self, uow: UnitOfWork, storage: Storage, ids: IdGenerator, max_bytes: int) -> None:
        self._uow = uow
        self._storage = storage
        self._ids = ids
        self._max_bytes = max_bytes

    def upload(
        self, scope: WorkspaceScope, stream: BinaryIO, request: UploadRequest
    ) -> tuple[UploadResponse, bool]:
        """Returns (response, created). `created=False` for a replay or duplicate content."""
        if request.idempotency_key:
            with self._uow(scope) as s:
                replay = self._replay(DatasetRepository(s), scope, request.idempotency_key)
            if replay is not None:
                return replay, False
        version_id = self._ids.new("dsv")
        key = raw_key(scope, version_id)
        source_sha256 = self._store(stream, key)
        try:
            return self._register(scope, request, version_id, source_sha256), True
        except _Existing as existing:
            self._storage.delete(key)
            return existing.response, False
        except IntegrityError:  # a concurrent upload of the same bytes won the unique key
            self._storage.delete(key)
        except Exception:
            self._storage.delete(key)  # nothing references the stored bytes; don't orphan them
            raise
        with self._uow(scope) as s:
            repo = DatasetRepository(s)
            version = repo.find_by_source(scope, source_sha256)
            if version is None:
                raise DataError("Upload conflicted; retry the request.", retryable=True)
            return _describe(repo, scope, version), False

    def _store(self, stream: BinaryIO, key: str) -> str:
        digest = hashlib.sha256()
        size = 0
        with self._storage.writer(key) as out:
            while chunk := stream.read(_CHUNK):
                if size == 0 and sniff_head(chunk) in ("pdf", "binary"):
                    raise reject(
                        UnsupportedFormatError,
                        "UNSUPPORTED_FORMAT",
                        "Only CSV and .xlsx files can be ingested as a table.",
                        hint="Upload a CSV or .xlsx file.",
                    )
                size += len(chunk)
                if size > self._max_bytes:
                    raise reject(
                        UploadTooLargeError,
                        "OVERSIZED_FILE",
                        "The file exceeds the upload size limit.",
                        limit_bytes=self._max_bytes,
                    )
                digest.update(chunk)
                out.write(chunk)
            if size == 0:
                raise reject(DataError, "EMPTY_FILE", "The file is empty.")
        return f"sha256:{digest.hexdigest()}"

    def _register(
        self, scope: WorkspaceScope, request: UploadRequest, version_id: str, source_sha256: str
    ) -> UploadResponse:
        filename = safe_filename(request.filename)
        with self._uow(scope) as s:
            repo = DatasetRepository(s)
            existing = repo.find_by_source(scope, source_sha256)
            if existing is not None:
                raise _Existing(_describe(repo, scope, existing))
            if request.dataset_id is None:
                dataset_id = self._ids.new("ds")
                name = (request.name or filename or "").strip()[:255] or "Untitled dataset"
                repo.create_dataset(scope, dataset_id, name, "file_upload")
            elif repo.get_dataset(scope, request.dataset_id) is None:
                raise NotFoundError("dataset not found")
            else:
                dataset_id = request.dataset_id
            version = repo.create_version(scope, version_id, dataset_id, source_sha256)
            payload = {"dataset_id": dataset_id, "version_id": version_id}
            job = JobRepository(s).enqueue(
                scope,
                self._ids.new("job"),
                JobType.INGEST,
                {**payload, "filename": filename, "sheet": request.sheet},
                idempotency_key=request.idempotency_key,
            )
            if job.payload.get("version_id") != version_id:  # lost the key race
                raise _Existing(self._from_job(repo, scope, job.id, job.payload))
            repo.add_outbox(scope, self._ids.new("evt"), "DatasetUploaded", payload)
        return UploadResponse(
            dataset_id=dataset_id, version_id=version_id, job_id=job.id, status=version.state
        )

    def _replay(
        self, repo: DatasetRepository, scope: WorkspaceScope, key: str
    ) -> UploadResponse | None:
        job = repo.job_for_key(scope, key)
        return None if job is None else self._from_job(repo, scope, *job)

    @staticmethod
    def _from_job(
        repo: DatasetRepository, scope: WorkspaceScope, job_id: str, payload: dict[str, Any]
    ) -> UploadResponse:
        version = repo.get_version(scope, str(payload.get("version_id")))
        if version is None:
            raise NotFoundError("the Idempotency-Key belongs to a different request")
        return UploadResponse(
            dataset_id=version.dataset_id,
            version_id=version.id,
            job_id=job_id,
            status=version.state,
        )
