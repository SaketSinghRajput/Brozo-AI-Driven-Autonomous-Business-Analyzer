"""``viz.spec`` tool (TASK-TOOL-007, TRD §21.2): a ChartSpec for one evidence record.

Reads the evidence and its stored result rows (workspace-scoped) and lets the deterministic
ChartSelector pick the chart the WidgetRegistry allows for the evidence's operation; a single
number becomes a ``kpi`` spec. The spec only references the evidence (no values), so the tool
creates no new evidence (``produces_evidence=False``), like ``evidence.get``, whose permission
(MEMORY_READ: evidence is analytical memory) it shares.
"""

import asyncio

from pydantic import BaseModel, ConfigDict

from app.core.errors import NotFoundError, ToolError
from app.core.scope import WorkspaceScope
from app.dashboard.charts import ChartSelector, chart_spec
from app.dashboard.domain import ChartSpec
from app.dashboard.registry import WidgetRegistry, WidgetType
from app.evidence.domain import Evidence, EvidenceId
from app.evidence.preview import RowsPreview
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork
from app.planner.domain import Unavailability
from app.tools.domain import ToolContext, ToolMetadata, ToolPermission


class VizSpecArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: EvidenceId
    prefer: WidgetType | None = None  # used when the registry allows it for this evidence


class VizSpecOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    widget_type: WidgetType
    chart: ChartSpec


class VizSpecTool:
    meta = ToolMetadata(
        name="viz.spec",
        version="1.0.0",
        description="Choose a chart for one evidence record of this workspace and return its "
        "ChartSpec (chart type from the result's shape; values stay in the evidence).",
        permission=ToolPermission.MEMORY_READ,
        side_effects="none",
        timeout_s=5,
        max_calls_per_turn=10,
        parallel_safe=True,
        produces_evidence=False,
        runs_in="api",
    )
    input_model: type[BaseModel] = VizSpecArgs
    output_model: type[BaseModel] = VizSpecOutput

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow
        self._registry = WidgetRegistry()
        self._selector = ChartSelector()

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability:
        a = VizSpecArgs.model_validate(args.model_dump())
        scope = WorkspaceScope(workspace_id=ctx.workspace_id, principal_id=ctx.principal_id)
        evidence, rows = await asyncio.to_thread(self._load, scope, a.evidence_id)
        allowed = self._registry.compatible(evidence.operation)
        if a.prefer is not None and a.prefer in allowed:
            allowed = (a.prefer,)
        widget = self._selector.choose(rows, allowed)
        if widget is None and isinstance(evidence.result.value, int | float):
            widget = "kpi_card"
        if widget is None:
            raise ToolError("the evidence has no result that can be charted")
        spec = chart_spec(
            widget,
            rows,
            evidence_id=evidence.evidence_id,
            source_operation=evidence.operation,
            unit=evidence.result.unit,
        )
        return VizSpecOutput(widget_type=widget, chart=spec)

    def _load(self, scope: WorkspaceScope, evidence_id: str) -> tuple[Evidence, RowsPreview | None]:
        with self._uow(scope) as s:
            repo = EvidenceRepository(s)
            evidence = repo.get(scope, evidence_id)
            _, rows = repo.rows(scope, evidence_id)
        if evidence is None:
            raise NotFoundError("evidence not found")
        return evidence, rows
