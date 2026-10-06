"""``POST /api/v1/analytics/query`` for dashboard filters and drill-down (TASK-ANALYSIS-016).

The request goes through the ``ToolExecutor`` as ``analytics.metric_query`` so it gets the same
guards and leaves the same trail (tool_run + evidence) as an LLM- or planner-initiated call. The
response returns the table and the evidence id; the compiled SQL stays in the evidence record
(the evidence drawer shows it, redacted for viewers without VIEW_QUERY, design §39).

Permissions: the route requires RUN_ANALYSIS; tool permissions are the caller's role ∩ the
``dashboard_system`` profile (it includes SQL_READ).
"""

from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.analytics.domain import MetricRequest, ResultTable
from app.analytics.tools.query import MetricQueryOutput
from app.api.deps import ContainerDep, PrincipalDep, require
from app.core.errors import (
    ApplicationError,
    AppValidationError,
    AuthorizationError,
    NotFoundError,
    RateLimitedError,
    SchemaError,
    SecurityError,
    ToolError,
)
from app.core.logging import trace_id_var
from app.core.scope import WorkspaceScope
from app.evidence.recorder import CallKey
from app.security.permissions import Permission
from app.tools.domain import ErrorInfo, ToolContext
from app.tools.executor import TurnBudget
from app.tools.permissions import TurnProfile, effective_permissions

router = APIRouter(prefix="/analytics", tags=["analytics"])
TOOL = "analytics.metric_query"
_ERRORS: dict[str, type[ApplicationError]] = {
    "SCHEMA_ERROR": SchemaError,
    "NOT_FOUND": NotFoundError,
    "AUTH_ERROR": AuthorizationError,
    "RATE_LIMITED": RateLimitedError,
    "SECURITY_ERROR": SecurityError,
    "VALIDATION_ERROR": AppValidationError,
}


class AnalyticsQueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    semantic_model_version: Annotated[int, Field(ge=1)]
    request: MetricRequest


class AnalyticsQueryResponse(BaseModel):
    table: ResultTable
    value: float | None
    unit: str | None
    truncated: bool
    evidence_id: str


def _raise(error: ErrorInfo | None) -> NoReturn:
    if error is None:
        raise ToolError("analytics query did not produce a result")
    raise _ERRORS.get(error.code, ToolError)(error.message, retryable=error.retryable)


@router.post("/query")
async def analytics_query(
    body: AnalyticsQueryBody,
    scope: Annotated[WorkspaceScope, Depends(require(Permission.RUN_ANALYSIS))],
    principal: PrincipalDep,
    container: ContainerDep,
) -> AnalyticsQueryResponse:
    ctx = ToolContext(
        workspace_id=scope.workspace_id,
        principal_id=scope.principal_id,
        trace_id=trace_id_var.get() or container.ids.new("trace"),
        permissions=effective_permissions(principal.role, TurnProfile.DASHBOARD_SYSTEM),
        dataset_version_id=body.dataset_version_id,
        semantic_model_version=body.semantic_model_version,
    )
    key = CallKey(execution_id=container.ids.new("apiq"), step_id=TOOL)
    args = body.request.model_dump(mode="json")
    result = await container.tool_executor.run(TOOL, args, ctx, key, TurnBudget(max_calls=1))
    if result.status != "ok" or result.output is None or result.evidence_id is None:
        _raise(result.error)
    output = MetricQueryOutput.model_validate(result.output)
    return AnalyticsQueryResponse(
        table=output.table,
        value=output.value,
        unit=output.unit,
        truncated=output.truncated,
        evidence_id=result.evidence_id,
    )
