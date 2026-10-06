"""Datasets API (TASK-DATA-010, TASK-DATA-012, TASK-PROFILE-009). Workspace-scoped.

POST /datasets is `async` on purpose: it checks `Content-Length` *before* the multipart body is
parsed and spooled (a FastAPI `UploadFile` parameter would read the body first), then runs the
blocking store/DB work in the thread pool (ADR-0022). Chunked uploads without a length are still
capped while streaming into the store.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.api.deps import ContainerDep, require
from app.core.errors import AppValidationError, NotFoundError
from app.core.scope import WorkspaceScope
from app.data.domain import Dataset, DatasetVersion, RejectedRow
from app.data.ingestion.errors import UploadTooLargeError, reject
from app.data.profiling.domain import ProfileResult
from app.data.profiling.profiler import ENGINE_VERSION
from app.data.repository import DatasetRepository
from app.data.upload import UploadRequest, UploadResponse, UploadService
from app.security.permissions import Permission

router = APIRouter(prefix="/datasets", tags=["datasets"])
ViewScopeDep = Annotated[WorkspaceScope, Depends(require(Permission.VIEW_DATASET))]
UploadScopeDep = Annotated[WorkspaceScope, Depends(require(Permission.UPLOAD_DATASET))]

_MULTIPART_OVERHEAD = 64 * 1024
_UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "name": {"type": "string", "maxLength": 255},
                        "dataset_id": {"type": "string", "pattern": r"^ds_\w+$"},
                        "sheet": {"type": "string", "maxLength": 255},
                    },
                }
            }
        },
    }
}


class QuarantineSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version_id: str
    rows_seen: int
    rows_rejected: int
    rejected_sample: list[RejectedRow]
    flags: list[str]


def _form_text(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 255:
        raise AppValidationError(f"invalid form field {field!r}")
    return value


@router.post(
    "",
    status_code=201,
    openapi_extra=_UPLOAD_BODY,
    responses={
        200: {"description": "Idempotency-Key replay or identical content (existing version)"},
        413: {"description": "SECURITY_ERROR(OVERSIZED_FILE)"},
        415: {"description": "DATA_ERROR(UNSUPPORTED_FORMAT)"},
    },
)
async def upload_dataset(
    request: Request,
    response: Response,
    scope: UploadScopeDep,
    container: ContainerDep,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=200, pattern=r"^[\x21-\x7e]+$")
    ] = None,
) -> UploadResponse:
    """Upload a CSV/.xlsx file. Side effects: stored raw file; dataset (unless `dataset_id` is
    given), version (UPLOADED), ingest job and DatasetUploaded outbox rows in one transaction."""
    limit = container.config.storage.max_upload_bytes
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit + _MULTIPART_OVERHEAD:
        raise reject(UploadTooLargeError, "OVERSIZED_FILE", "The upload exceeds the size limit.")
    form = await request.form(max_files=1, max_fields=4)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise AppValidationError("multipart field 'file' is required")
        dataset_id = _form_text(form.get("dataset_id"), "dataset_id")
        if dataset_id is not None and not dataset_id.startswith("ds_"):
            raise AppValidationError("invalid form field 'dataset_id'")
        upload_request = UploadRequest(
            filename=upload.filename,
            name=_form_text(form.get("name"), "name"),
            dataset_id=dataset_id,
            sheet=_form_text(form.get("sheet"), "sheet"),
            idempotency_key=idempotency_key,
        )
        service = UploadService(container.uow, container.storage, container.ids, limit)
        result, created = await run_in_threadpool(
            service.upload, scope, upload.file, upload_request
        )
    finally:
        await form.close()
    response.status_code = 201 if created else 200
    return result


@router.get("")
def list_datasets(
    scope: ViewScopeDep,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Dataset]:
    with container.uow(scope) as s:
        return DatasetRepository(s).list_datasets(scope, limit, offset)


@router.get("/{dataset_id}")
def get_dataset(dataset_id: str, scope: ViewScopeDep, container: ContainerDep) -> Dataset:
    with container.uow(scope) as s:
        dataset = DatasetRepository(s).get_dataset(scope, dataset_id)
    if dataset is None:
        raise NotFoundError("dataset not found")
    return dataset


@router.get("/{dataset_id}/versions")
def list_versions(
    dataset_id: str, scope: ViewScopeDep, container: ContainerDep
) -> list[DatasetVersion]:
    with container.uow(scope) as s:
        repo = DatasetRepository(s)
        if repo.get_dataset(scope, dataset_id) is None:
            raise NotFoundError("dataset not found")
        return repo.list_versions(scope, dataset_id)


def _version(
    repo: DatasetRepository, scope: WorkspaceScope, dataset_id: str, version_id: str
) -> DatasetVersion:
    version = repo.get_version(scope, version_id)
    if version is None or version.dataset_id != dataset_id:
        raise NotFoundError("dataset version not found")
    return version


@router.get("/{dataset_id}/versions/{version_id}")
def get_version(
    dataset_id: str, version_id: str, scope: ViewScopeDep, container: ContainerDep
) -> DatasetVersion:
    with container.uow(scope) as s:
        return _version(DatasetRepository(s), scope, dataset_id, version_id)


@router.get("/{dataset_id}/versions/{version_id}/quarantine")
def get_quarantine(
    dataset_id: str, version_id: str, scope: ViewScopeDep, container: ContainerDep
) -> QuarantineSample:
    """Malformed-row sample: row numbers and reasons only, never row content."""
    with container.uow(scope) as s:
        repo = DatasetRepository(s)
        version = _version(repo, scope, dataset_id, version_id)
        report = repo.ingest_report(scope, version_id)
    if report is None:
        raise NotFoundError("the version has not been ingested")
    return QuarantineSample(
        version_id=version.id,
        rows_seen=report["rows_seen"],
        rows_rejected=report["rows_rejected"],
        rejected_sample=report["rejected_sample"],
        flags=version.review_flags,
    )


@router.get("/{dataset_id}/versions/{version_id}/profile")
def get_profile(
    dataset_id: str, version_id: str, scope: ViewScopeDep, container: ContainerDep
) -> ProfileResult:
    with container.uow(scope) as s:
        repo = DatasetRepository(s)
        _version(repo, scope, dataset_id, version_id)
        profile = repo.get_profile(scope, version_id, ENGINE_VERSION)
    if profile is None:
        raise NotFoundError("the version has not been profiled yet")
    return ProfileResult.model_validate(profile)
