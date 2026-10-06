"""Dashboard read routes (TASK-DASH-005/006), VIEW_DATASET, workspace scoped.

- ``GET /datasets/{dataset_id}/versions/{version_id}/dashboard``: the DashboardSpec built for the
  version's current semantic model; 404 until the ``run_plan`` job has built it (the version is
  then READY), and for ids of another workspace.
- ``GET /datasets/{dataset_id}/versions/{version_id}/dimensions/{name}/values?limit=``: distinct
  values (<= 200) of a semantic dimension, for filter pickers.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import ContainerDep, require
from app.core.errors import NotFoundError
from app.core.scope import WorkspaceScope
from app.dashboard.dimensions import MAX_VALUES, DimensionValues, DimensionValuesService
from app.dashboard.domain import DashboardSpec
from app.dashboard.service import DashboardService
from app.data.repository import DatasetRepository
from app.planner.runs import AnalysisRunRepository, AnalysisRunSummary
from app.security.permissions import Permission
from app.validator.service import ValidationService

router = APIRouter(prefix="/datasets", tags=["dashboard"])
ViewScope = Annotated[WorkspaceScope, Depends(require(Permission.VIEW_DATASET))]


@router.get("/{dataset_id}/versions/{version_id}/dashboard")
def get_dashboard(
    dataset_id: str,
    version_id: str,
    scope: ViewScope,
    container: ContainerDep,
) -> DashboardSpec:
    validation = ValidationService(container.uow, container.ids)
    service = DashboardService(container.uow, validation, container.clock)
    return service.get(scope, dataset_id, version_id)


@router.get("/{dataset_id}/versions/{version_id}/analysis-runs")
def list_analysis_runs(
    dataset_id: str, version_id: str, scope: ViewScope, container: ContainerDep
) -> list[AnalysisRunSummary]:
    """The version's analysis runs, newest first (state, timestamps, step counts)."""
    with container.uow(scope) as s:
        version = DatasetRepository(s).get_version(scope, version_id)
        if version is None or version.dataset_id != dataset_id:
            raise NotFoundError("dataset version not found")
        return AnalysisRunRepository(s).summaries(scope, version_id)


@router.get("/{dataset_id}/versions/{version_id}/dimensions/{name}/values")
def get_dimension_values(
    dataset_id: str,
    version_id: str,
    name: str,
    scope: ViewScope,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=MAX_VALUES)] = MAX_VALUES,
) -> DimensionValues:
    service = DimensionValuesService(container.uow, container.storage)
    return service.values(scope, dataset_id, version_id, name, limit)
