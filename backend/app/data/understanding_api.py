"""Understanding, domain, semantic-model and capability endpoints (TASK-SCHEMA-010,
TASK-DOMAIN-007, TASK-SEM-009, TASK-ANALYSIS-002). Workspace-scoped; reads need VIEW_DATASET,
changes need EDIT_SEMANTICS.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, require
from app.core.errors import NotFoundError
from app.core.scope import WorkspaceScope
from app.data.override_service import OverrideInput, OverrideService
from app.data.repository import DatasetRepository
from app.data.semantic.domain import SemanticModel
from app.data.understanding.domain import DomainClassification, Understanding
from app.data.understanding_store import DomainSource, UnderstandingStore
from app.planner.domain import CapabilityReport
from app.planner.service import PlanningService
from app.security.permissions import Permission
from app.validator.confidence import confidence_level

router = APIRouter(prefix="/datasets", tags=["understanding"])
ViewScopeDep = Annotated[WorkspaceScope, Depends(require(Permission.VIEW_DATASET))]
EditScopeDep = Annotated[WorkspaceScope, Depends(require(Permission.EDIT_SEMANTICS))]
ModelVersion = Annotated[int | None, Query(ge=1)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UnderstandingView(_Body):
    understanding: Understanding
    domain: DomainClassification | None  # None until DOMAIN_IDENTIFIED
    domain_source: DomainSource | None
    review_flags: list[str]
    # domain.confidence in the validator's bands (high >= 0.8, medium >= 0.5, else low)
    domain_confidence_level: Literal["high", "medium", "low"] | None = None


class DomainOverrideRequest(_Body):
    domain: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]


class DomainOverrideResponse(_Body):
    version_id: str
    domain: DomainClassification
    semantic_model_version: int | None  # the re-mapped model; None when not mapped yet


class SemanticModelPatch(_Body):
    base_version: Annotated[int, Field(ge=1)]  # optimistic concurrency: must be current
    overrides: Annotated[list[OverrideInput], Field(min_length=1, max_length=50)]


def _flags(repo: DatasetRepository, scope: WorkspaceScope, dataset_id: str, v: str) -> list[str]:
    version = repo.get_version(scope, v)
    if version is None or version.dataset_id != dataset_id:
        raise NotFoundError("dataset version not found")
    return version.review_flags


@router.get("/{dataset_id}/versions/{version_id}/understanding")
def get_understanding(
    dataset_id: str, version_id: str, scope: ViewScopeDep, container: ContainerDep
) -> UnderstandingView:
    with container.uow(scope) as s:
        flags = _flags(DatasetRepository(s), scope, dataset_id, version_id)
        store = UnderstandingStore(s)
        understanding = store.latest(scope, version_id)
        domain = store.domain(scope, version_id)
    if understanding is None:
        raise NotFoundError("the version has not been analysed yet")
    return UnderstandingView(
        understanding=understanding,
        domain=domain.classification if domain else None,
        domain_source=domain.source if domain else None,
        review_flags=flags,
        domain_confidence_level=confidence_level(domain.classification.confidence)
        if domain
        else None,
    )


@router.put("/{dataset_id}/domain")
def override_domain(
    dataset_id: str, body: DomainOverrideRequest, scope: EditScopeDep, container: ContainerDep
) -> DomainOverrideResponse:
    """Set the domain of the dataset's latest version (always wins; audited); re-maps it and
    enqueues a re-plan. Later versions of the dataset keep the override."""
    service = OverrideService(
        container.uow, container.ids, container.clock, container.auth.audit_keys
    )
    result = service.set_domain(scope, dataset_id, body.domain)
    return DomainOverrideResponse(
        version_id=result.version_id,
        domain=result.domain,
        semantic_model_version=result.semantic_model_version,
    )


@router.get("/{dataset_id}/versions/{version_id}/semantic-model")
def get_semantic_model(
    dataset_id: str,
    version_id: str,
    scope: ViewScopeDep,
    container: ContainerDep,
    version: ModelVersion = None,
) -> SemanticModel:
    """The current semantic model, or the given model ``version`` (every version is kept)."""
    with container.uow(scope) as s:
        _flags(DatasetRepository(s), scope, dataset_id, version_id)
        model = UnderstandingStore(s).semantic_model(scope, version_id, version)
    if model is None:
        raise NotFoundError("semantic model not found")
    return model


@router.patch("/{dataset_id}/versions/{version_id}/semantic-model")
def patch_semantic_model(
    dataset_id: str,
    version_id: str,
    body: SemanticModelPatch,
    scope: EditScopeDep,
    container: ContainerDep,
) -> SemanticModel:
    """Apply overrides -> a new model version (audited) -> re-plan. 409 if ``base_version`` is
    stale; 422 if an override cannot be applied."""
    service = OverrideService(
        container.uow, container.ids, container.clock, container.auth.audit_keys
    )
    return service.patch_semantic_model(
        scope, dataset_id, version_id, body.base_version, body.overrides
    )


@router.get("/{dataset_id}/versions/{version_id}/capabilities")
def get_capabilities(
    dataset_id: str,
    version_id: str,
    scope: ViewScopeDep,
    container: ContainerDep,
    semantic_model_version: ModelVersion = None,
) -> CapabilityReport:
    """Which analyses the data supports, and for each that it does not: "Insufficient data" with
    the specific missing requirement and what would enable it."""
    service = PlanningService(container.uow, container.tools)
    return service.capabilities(scope, dataset_id, version_id, semantic_model_version)
