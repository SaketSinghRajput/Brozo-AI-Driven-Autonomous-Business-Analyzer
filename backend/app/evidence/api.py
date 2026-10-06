"""Evidence and claim read API (TASK-EVID-005, design 39, PRD-EVD-002). Workspace-scoped.

Reading requires VIEW_DATASET. SQL text and query parameters are redacted unless the
caller's role also holds VIEW_QUERY (Analyst and Admin).
Records of another workspace answer 404, like unknown ids.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import ContainerDep, PrincipalDep, require
from app.core.errors import NotFoundError
from app.core.scope import WorkspaceScope
from app.evidence.claims import ClaimRepository
from app.evidence.domain import Claim, Contract, Evidence
from app.evidence.preview import EvidenceData, as_data
from app.evidence.repository import EvidenceRepository
from app.security.permissions import Permission, has_permission

REDACTED = "[redacted: requires VIEW_QUERY]"

router = APIRouter(tags=["evidence"])


def can_view_query(principal: PrincipalDep) -> bool:
    return has_permission(principal.role, Permission.VIEW_QUERY)


ViewQueryDep = Annotated[bool, Depends(can_view_query)]
ViewScopeDep = Annotated[WorkspaceScope, Depends(require(Permission.VIEW_DATASET))]


class ClaimView(Contract):
    """The claim with its evidence embedded (design 39 read model)."""

    claim: Claim
    evidence: list[Evidence]


def redact(evidence: Evidence, view_query: bool) -> Evidence:
    if view_query or evidence.query is None:
        return evidence
    query = evidence.query.model_copy(update={"sql": REDACTED, "params": {}})
    return evidence.model_copy(update={"query": query})


@router.get("/evidence/{evidence_id}")
def get_evidence(
    evidence_id: str, scope: ViewScopeDep, container: ContainerDep, view_query: ViewQueryDep
) -> Evidence:
    with container.uow(scope) as s:
        evidence = EvidenceRepository(s).get(scope, evidence_id)
    if evidence is None:
        raise NotFoundError("evidence not found")
    return redact(evidence, view_query)


@router.get("/evidence/{evidence_id}/data")
def get_evidence_data(
    evidence_id: str, scope: ViewScopeDep, container: ContainerDep
) -> EvidenceData:
    """The stored result rows behind an evidence record (<= 500), for chart and table widgets.
    Same permission and scope as the evidence itself; rows hold no SQL, so nothing is redacted."""
    with container.uow(scope) as s:
        found, rows = EvidenceRepository(s).rows(scope, evidence_id)
    if not found:
        raise NotFoundError("evidence not found")
    return as_data(evidence_id, rows)


@router.get("/claims/{claim_id}")
def get_claim(
    claim_id: str, scope: ViewScopeDep, container: ContainerDep, view_query: ViewQueryDep
) -> ClaimView:
    with container.uow(scope) as s:
        claim = ClaimRepository(s).get(scope, claim_id)
        cited = EvidenceRepository(s).get_many(scope, claim.evidence_ids) if claim else []
    if claim is None:
        raise NotFoundError("claim not found")
    return ClaimView(claim=claim, evidence=[redact(e, view_query) for e in cited])
