"""Shared plumbing for analytics tools (TASK-TOOL-006, design §19).

A tool loads the context's dataset version + semantic model (workspace-scoped), then runs
``compute`` in a worker thread (DuckDB and Parquet reads are blocking). Tools never write
evidence: the ``ToolExecutor`` does, through ``EvidenceRecorder``, from the output's
``value``/``unit``/``rows``, ``query`` (engine, SQL, params) and ``calculation`` fields.
"""

import asyncio
from datetime import date

from pydantic import BaseModel, ConfigDict, JsonValue

from app.analytics.source import DatasetSource
from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.sql.resolver import SemanticResolver
from app.analytics.sql.runner import MetricQueryRunner
from app.core.errors import ToolError
from app.core.scope import WorkspaceScope
from app.data.domain import Cell
from app.planner.domain import Filter, Unavailability
from app.tools.domain import ToolContext, ToolMetadata, ToolPermission

QueryDict = dict[str, JsonValue]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def tool_meta(
    name: str, description: str, permission: ToolPermission, timeout_s: float = 30
) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        version="1.0.0",
        description=description,
        permission=permission,
        side_effects="none",
        timeout_s=timeout_s,
        max_calls_per_turn=8,
        parallel_safe=True,
        runs_in="api",
    )


def expect[T: BaseModel](args: BaseModel, model: type[T]) -> T:
    """The executor already validated ``args`` against the tool's input model."""
    if not isinstance(args, model):
        raise ToolError("unexpected tool arguments")
    return args


def number(cell: Cell) -> float | None:
    return float(cell) if isinstance(cell, int | float) and not isinstance(cell, bool) else None


def range_filters(
    runner: MetricQueryRunner, time_dimension: str | None, start: date | None, end: date | None
) -> list[Filter]:
    """A date range as filters on the (resolved) time dimension, without a period column."""
    if start is None and end is None:
        return []
    name = SemanticResolver(runner.model).time_dimension(time_dimension).name
    bounds = [Filter(dimension=name, op="gte", value=start)] if start else []
    return bounds + ([Filter(dimension=name, op="lte", value=end)] if end else [])


class AnalyticsTool:
    meta: ToolMetadata
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    def __init__(self, source: DatasetSource, engine: AnalyticalEngine) -> None:
        self._source = source
        self._engine = engine

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability:
        return await asyncio.to_thread(self._load_and_compute, args, ctx)

    def _load_and_compute(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        if ctx.dataset_version_id is None or ctx.semantic_model_version is None:
            raise ToolError("analytics tools need a dataset version and semantic model version")
        scope = WorkspaceScope(workspace_id=ctx.workspace_id, principal_id=ctx.principal_id)
        loaded = self._source.load(scope, ctx.dataset_version_id, ctx.semantic_model_version)
        return self.compute(args, MetricQueryRunner(self._engine, loaded.model, loaded.table))

    def compute(self, args: BaseModel, runner: MetricQueryRunner) -> BaseModel:
        raise NotImplementedError
