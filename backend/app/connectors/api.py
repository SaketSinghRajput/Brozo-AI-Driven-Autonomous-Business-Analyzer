"""Connectors API (TASK-DATA-013). Workspace-scoped; responses never contain credentials."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import ContainerDep, require
from app.connectors.service import Connector, ConnectorCreate, ConnectorService
from app.core.scope import WorkspaceScope
from app.jobs.models import Job
from app.security.permissions import Permission

router = APIRouter(prefix="/connectors", tags=["connectors"])
UploadScopeDep = Annotated[WorkspaceScope, Depends(require(Permission.UPLOAD_DATASET))]
ViewScopeDep = Annotated[WorkspaceScope, Depends(require(Permission.VIEW_DATASET))]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_connector(
    body: ConnectorCreate, scope: UploadScopeDep, container: ContainerDep
) -> Connector:
    """Register a PostgreSQL source (one table or one SELECT). The host must be allow-listed
    (`APP_CONNECTORS__ALLOWED_HOSTS`); the password is stored encrypted and never returned."""
    return ConnectorService.from_container(container).create(scope, body)


@router.get("")
def list_connectors(scope: ViewScopeDep, container: ContainerDep) -> list[Connector]:
    return ConnectorService.from_container(container).list(scope)


@router.post("/{connector_id}/import", status_code=status.HTTP_202_ACCEPTED)
def import_connector(connector_id: str, scope: UploadScopeDep, container: ContainerDep) -> Job:
    """Enqueue a snapshot import; poll GET /jobs/{id}. Its result is the new dataset version,
    which then follows the upload pipeline (ingest -> profile -> understand)."""
    return ConnectorService.from_container(container).enqueue_import(scope, connector_id)
