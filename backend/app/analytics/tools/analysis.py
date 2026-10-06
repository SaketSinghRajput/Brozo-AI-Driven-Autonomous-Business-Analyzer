"""``analytics.stats``, ``analytics.trend``, ``analytics.explain_change`` (TRD §21.2, §28)."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.analytics.domain import MetricRequest, SemanticName
from app.analytics.services.root_cause import RootCauseRequest, RootCauseResult, RootCauseService
from app.analytics.services.statistics import Correlation, Description, correlate, describe
from app.analytics.services.timeseries import FillPolicy, Series, TrendTest, fill_calendar
from app.analytics.services.timeseries import trend_test as mann_kendall
from app.analytics.sql.periods import Grain
from app.analytics.sql.runner import MetricQueryRunner
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


class StatsArgs(Args):
    metric: SemanticName
    by: SemanticName  # the dimension whose members form the sample
    metric_b: SemanticName | None = None  # correlate metric vs metric_b across members
    method: Literal["pearson", "spearman"] = "pearson"
    start: date | None = None
    end: date | None = None
    time_dimension: SemanticName | None = None
    filters: Annotated[list[Filter], Field(default_factory=list, max_length=20)]


class StatsOutput(Output):
    description: Description
    correlation: Correlation | None
    unit: str | None
    rows: int
    query: QueryDict
    queries: list[QueryDict]


class StatsTool(AnalyticsTool):
    meta = tool_meta(
        "analytics.stats",
        "Describe a metric's distribution across the members of a dimension (n, mean, std, "
        "quartiles); optionally correlate it with a second metric across the same members.",
        ToolPermission.PYTHON_ANALYTICS,
    )
    input_model: type[BaseModel] = StatsArgs
    output_model: type[BaseModel] = StatsOutput

    def compute(self, args: BaseModel, runner: MetricQueryRunner) -> BaseModel:
        a = expect(args, StatsArgs)
        filters = [*a.filters, *range_filters(runner, a.time_dimension, a.start, a.end)]

        def by_member(metric: str) -> tuple[dict[str, float | None], QueryDict, str | None]:
            run = runner.run(
                MetricRequest(metric=metric, dimensions=[a.by], filters=filters, limit=10_000)
            )
            values = {str(r[0]): number(r[1]) for r in run.table.rows}
            return values, run.record(), run.table.columns[-1].unit

        values, query, unit = by_member(a.metric)
        queries = [query]
        correlation = None
        if a.metric_b is not None:
            other, other_query, _ = by_member(a.metric_b)
            queries.append(other_query)
            keys = sorted(set(values) | set(other))
            correlation = correlate(
                [values.get(k) for k in keys], [other.get(k) for k in keys], a.method
            )
        return StatsOutput(
            description=describe(list(values.values())),
            correlation=correlation,
            unit=unit,
            rows=len(values),
            query=query,
            queries=queries,
        )


class TrendArgs(Args):
    metric: SemanticName
    grain: Grain = "month"
    start: date
    end: date
    time_dimension: SemanticName | None = None
    filters: Annotated[list[Filter], Field(default_factory=list, max_length=20)]
    fill_policy: FillPolicy = "none"
    alpha: Annotated[float, Field(gt=0, lt=0.5)] = 0.05


class TrendOutput(Output):
    series: Series
    trend: TrendTest
    value: float | None  # Sen's slope: metric change per period
    unit: str | None
    rows: int
    calculation: str
    query: QueryDict


class TrendTool(AnalyticsTool):
    meta = tool_meta(
        "analytics.trend",
        "Metric series per calendar period with missing periods reported (explicit fill policy) "
        "and a Mann-Kendall trend test with Sen's slope.",
        ToolPermission.PYTHON_ANALYTICS,
    )
    input_model: type[BaseModel] = TrendArgs
    output_model: type[BaseModel] = TrendOutput

    def compute(self, args: BaseModel, runner: MetricQueryRunner) -> BaseModel:
        a = expect(args, TrendArgs)
        run = runner.run(
            MetricRequest(
                metric=a.metric,
                filters=a.filters,
                time=TimeSpec(grain=a.grain, start=a.start, end=a.end),
                time_dimension=a.time_dimension,
                limit=10_000,
            )
        )
        points = [(date.fromisoformat(str(r[0])), number(r[1])) for r in run.table.rows]
        series = fill_calendar(points, a.grain, a.start, a.end, a.fill_policy)
        trend = mann_kendall([p.value for p in series.points], a.alpha)
        return TrendOutput(
            series=series,
            trend=trend,
            value=trend.sen_slope,
            unit=run.table.columns[-1].unit,
            rows=len(series.points),
            calculation="Mann-Kendall S with tie-corrected variance; Sen's slope per period",
            query=run.record(),
        )


class ExplainChangeOutput(Output):
    result: RootCauseResult
    value: float | None  # the observed change of the metric
    unit: str | None
    rows: int  # number of findings
    query: QueryDict
    calculation: str


class ExplainChangeTool(AnalyticsTool):
    meta = tool_meta(
        "analytics.explain_change",
        "Explain a metric change between a date range and its comparison range: ranked "
        "contributing factors (decomposition by dimension members, temporal precedence, "
        "association). Reports contributions and associations, never causes.",
        ToolPermission.PYTHON_ANALYTICS,
        timeout_s=90,
    )
    input_model: type[BaseModel] = RootCauseRequest
    output_model: type[BaseModel] = ExplainChangeOutput

    def compute(self, args: BaseModel, runner: MetricQueryRunner) -> BaseModel:
        result = RootCauseService(runner).explain(expect(args, RootCauseRequest))
        return ExplainChangeOutput(
            result=result,
            value=result.delta,
            unit=result.unit,
            rows=len(result.findings),
            query=result.queries[0],
            calculation="share_i = delta_i / sum(delta); score = |share| + precedence + |r|",
        )
