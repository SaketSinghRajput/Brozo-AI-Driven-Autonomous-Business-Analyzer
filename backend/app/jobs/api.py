"""Jobs API (TASK-JOB-003): poll status, request cancellation. Workspace-scoped."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import ContainerDep, require
from app.core.errors import NotFoundError
from app.core.scope import WorkspaceScope
from app.jobs.models import Job
from app.jobs.repository import JobRepository
from app.security.permissions import Permission

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Any member may watch job progress; cancelling stops shared work, so Analyst+ only.
ViewScope = Annotated[WorkspaceScope, Depends(require(Permission.VIEW_DATASET))]
CancelScope = Annotated[WorkspaceScope, Depends(require(Permission.UPLOAD_DATASET))]


@router.get("/{job_id}")
def get_job(job_id: str, scope: ViewScope, container: ContainerDep) -> Job:
    with container.uow(scope) as s:
        job = JobRepository(s).get(scope, job_id)
    if job is None:
        raise NotFoundError("job not found")
    return job


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, scope: CancelScope, container: ContainerDep) -> Job:
    with container.uow(scope) as s:
        return JobRepository(s).request_cancel(scope, job_id)
