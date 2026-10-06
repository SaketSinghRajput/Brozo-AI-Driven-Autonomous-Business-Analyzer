"""``analytics.metric_query``, ``analytics.growth``, ``analytics.rank`` (TRD §21.2)."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.analytics.domain import CompareTo, MetricRequest, ResultTable, SemanticName
from app.analytics.services.growth import GrowthResult, growth
from app.analytics.services.ranking import RankingResult, RankMethod, rank
from app.analytics.sql.runner import MetricQueryRunner, QueryRun
from app.analytics.tools.base import (
    AnalyticsTool,
    Args,
    Output,
    QueryDict,
    expect,
    number,
    range_filters,
    tool_meta,
)
from app.planner.domain import Filter, TimeSpec
from app.tools.domain import ToolPermission


class MetricQueryOutput(Output):
    table: ResultTable
    value: float | None  # set only when the result is one ungrouped row
    unit: str | None
    rows: int
    truncated: bool
    query: QueryDict


def metric_query_output(run: QueryRun) -> MetricQueryOutput:
    table = run.table
    value_col = table.first_of_role("value")
    grouped = any(c.role in {"dimension", "period"} for c in table.columns)
    single = len(table.rows) == 1 and not grouped
    return MetricQueryOutput(
        table=table,
        value=number(table.column("value")[0]) if single else None,
        unit=value_col.unit if value_col else None,
        rows=table.row_count,
        truncated=table.truncated,
        query=run.record(),
    )


class MetricQueryTool(AnalyticsTool):
    meta = tool_meta(
        "analytics.metric_query",
        "Compute a semantic metric, optionally grouped by dimensions, filtered, over a time range "
        "with a time grain, or compared with the previous period / same period last year.",
        ToolPermission.SQL_READ,
    )
    input_model: type[BaseModel] = MetricRequest
    output_model: type[BaseModel] = MetricQueryOutput

    def compute(self, args: BaseModel, runner: MetricQueryRunner) -> BaseModel:
        return metric_query_output(runner.run(expect(args, MetricRequest)))


class GrowthArgs(Args):
    metric: SemanticName
    start: date
    end: date
    compare_to: CompareTo = "previous_period"
    time_dimension: SemanticName | None = None
    filters: Annotated[list[Filter], Field(default_factory=list, max_length=20)]


class GrowthOutput(Output):
    metric: str
    growth: GrowthResult
    value: float | None
    unit: Literal["pct", "pp"]
    metric_unit: str | None
    compare_range: tuple[date, date] | None
    calculation: str
    query: QueryDict


class GrowthTool(AnalyticsTool):
    meta = tool_meta(
        "analytics.growth",
        "Growth of a metric from its comparison range to [start, end]: percent change (pct), or "
        "percentage points (pp) for percentage metrics. Undefined (null) when the base is 0.",
        ToolPermission.PYTHON_ANALYTICS,
    )
    input_model: type[BaseModel] = GrowthArgs
    output_model: type[BaseModel] = GrowthOutput

    def compute(self, args: BaseModel, runner: MetricQueryRunner) -> BaseModel:
        a = expect(args, GrowthArgs)
        run = runner.run(
            MetricRequest(
                metric=a.metric,
                filters=a.filters,
                time=TimeSpec(grain="day", start=a.start, end=a.end),
                time_dimension=a.time_dimension,
                compare_to=a.compare_to,
            )
        )
        current, base = run.table.rows[0] if run.table.rows else [None, None]
        metric_unit = run.table.columns[0].unit
        result = growth(number(base), number(current), metric_unit)
        return GrowthOutput(
            metric=a.metric,
            growth=result,
            value=result.value,
            unit=result.unit,
            metric_unit=metric_unit,
            compare_range=run.compiled.compare_range,
            calculation=("x1 - x0" if result.unit == "pp" else "(x1 - x0) / |x0|")
            + (f"; undefined: {result.undefined}" if result.undefined else ""),
            query=run.record(),
        )


class RankArgs(Args):
    metric: SemanticName
    dimension: SemanticName
    start: date | None = None
    end: date | None = None
    time_dimension: SemanticName | None = None
    filters: Annotated[list[Filter], Field(default_factory=list, max_length=20)]
    method: RankMethod = "competition"
    descending: bool = True
    top_n: Annotated[int, Field(ge=1, le=1000)] = 10


class RankOutput(Output):
    ranking: RankingResult
    unit: str | None
    rows: int
    query: QueryDict


class RankTool(AnalyticsTool):
    meta = tool_meta(
        "analytics.rank",
        "Rank the members of one dimension by a metric over an optional date range (explicit "
        "ties, top N).",
        ToolPermission.PYTHON_ANALYTICS,
    )
    input_model: type[BaseModel] = RankArgs
    output_model: type[BaseModel] = RankOutput

    def compute(self, args: BaseModel, runner: MetricQueryRunner) -> BaseModel:
        a = expect(args, RankArgs)
        bounds = range_filters(runner, a.time_dimension, a.start, a.end)
        run = runner.run(
            MetricRequest(
                metric=a.metric,
                dimensions=[a.dimension],
                filters=[*a.filters, *bounds],
                limit=10_000,
            )
        )
        ranking = rank(
            [(str(r[0]), number(r[1])) for r in run.table.rows],
            a.method,
            a.descending,
            a.top_n,
        )
        value_col = run.table.first_of_role("value")
        return RankOutput(
            ranking=ranking,
            unit=value_col.unit if value_col else None,
            rows=len(ranking.items),
            query=run.record(),
        )
