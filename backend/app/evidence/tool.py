"""``evidence.get`` tool (TASK-TOOL-008, TRD 21.2): read one evidence record, scoped, read-only.

The LLM gets a summary only: no SQL, no query parameters (they may hold data values), so the
tool needs no VIEW_QUERY check. Reading evidence creates no new evidence
(``produces_evidence=False``). Permission MEMORY_READ: evidence belongs to analytical memory and
the enum has no dedicated evidence permission.
"""

import asyncio
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.errors import NotFoundError
from app.core.scope import WorkspaceScope
from app.evidence.domain import Evidence, EvidenceId
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork
from app.planner.domain import Unavailability
from app.tools.domain import ToolContext, ToolMetadata, ToolPermission


class EvidenceGetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: EvidenceId


class EvidenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    operation: str
    dataset_version_id: str | None
    semantic_model_version: Annotated[int, Field(ge=1)] | None
    calculation: str | None
    value: float | int | None
    unit: str | None
    rows: int | None
    created_at: AwareDatetime


class EvidenceGetTool:
    meta = ToolMetadata(
        name="evidence.get",
        version="1.0.0",
        description="Read one evidence record of this workspace by id: operation, dataset "
        "version, calculation and result. Read-only; never returns SQL.",
        permission=ToolPermission.MEMORY_READ,
        side_effects="none",
        timeout_s=5,
        max_calls_per_turn=10,
        parallel_safe=True,
        produces_evidence=False,
        runs_in="api",
    )
    input_model: type[BaseModel] = EvidenceGetArgs
    output_model: type[BaseModel] = EvidenceSummary

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability:
        evidence_id = EvidenceGetArgs.model_validate(args.model_dump()).evidence_id
        scope = WorkspaceScope(workspace_id=ctx.workspace_id, principal_id=ctx.principal_id)
        evidence = await asyncio.to_thread(self._get, scope, evidence_id)
        if evidence is None:
            raise NotFoundError("evidence not found")
        return EvidenceSummary(
            evidence_id=evidence.evidence_id,
            operation=evidence.operation,
            dataset_version_id=evidence.dataset_version_id,
            semantic_model_version=evidence.semantic_model_version,
            calculation=evidence.calculation,
            value=evidence.result.value,
            unit=evidence.result.unit,
            rows=evidence.result.rows,
            created_at=evidence.created_at,
        )

    def _get(self, scope: WorkspaceScope, evidence_id: str) -> Evidence | None:
        with self._uow(scope) as s:
            return EvidenceRepository(s).get(scope, evidence_id)
