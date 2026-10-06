"""Plan template library (TASK-ANALYSIS-004; design 17 ``PlanTemplateLibrary``, TRD 14.3).

Deterministic step templates per ``AnalysisIntent.objective``. A template only fills tool
arguments from the semantic model and the data facts (metric/dimension names, date ranges, grain);
it never invents numbers. Arguments follow the registered tools' input models
(app.analytics.tools: MetricRequest, TrendArgs, GrowthArgs, RankArgs, RootCauseRequest) and the
``ml.*`` tools (app.ml: ForecastArgs, AnomalyArgs, SegmentsArgs; they take stored column names,
and the dataset version comes from the ToolContext). ``PlanValidator`` checks them strictly
against whatever registry is in use. Steps of one template are independent
(the analytics tools read the data themselves), so templates declare no dependencies yet.
Objectives without a template (predict, simulate, document_lookup) return no steps.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from pydantic import JsonValue

from app.planner.config import AnalysisConfig
from app.planner.facts import DataFacts, period_start

Validation = Literal["schema", "numeric", "model", "logic"]


@dataclass(frozen=True)
class StepDraft:
    type: str  # also names the capability the step needs (PlanValidator.STEP_ANALYSIS)
    tool: str
    args: dict[str, JsonValue]
    expected_output: str
    validation: list[Validation] = field(default_factory=lambda: ["schema", "numeric"])


@dataclass(frozen=True)
class TemplateContext:
    facts: DataFacts
    metrics: list[str]  # KPI metrics (metrics first, then measures), primary first
    series_metric: str | None  # a summable metric for time series (trend/forecast/RCA)
    dimensions: list[str]  # categorical dimensions to compare/rank/explain by
    seed: int
    horizon: int | None = None  # forecast horizon at the planning grain


Template = Callable[[TemplateContext], list[StepDraft]]


def _time_args(ctx: TemplateContext) -> dict[str, JsonValue]:
    t = ctx.facts.time
    return {} if t is None else {"time_dimension": t.dimension}


def _overview(ctx: TemplateContext) -> list[StepDraft]:
    return [
        StepDraft("aggregate", "analytics.metric_query", {"metric": m}, "MetricQueryOutput")
        for m in ctx.metrics
    ]


def _metric_value(ctx: TemplateContext) -> list[StepDraft]:
    return _overview(ctx)[:1]


def _trend(ctx: TemplateContext) -> list[StepDraft]:
    t, metric = ctx.facts.time, ctx.series_metric
    if t is None or metric is None:
        return []
    last = period_start(t.end, t.planning_grain)
    common: dict[str, JsonValue] = {"metric": metric, **_time_args(ctx)}
    series = {**common, "grain": t.planning_grain, "start": str(t.start), "end": str(t.end)}
    growth = {**common, "start": str(last), "end": str(t.end), "compare_to": "previous_period"}
    return [
        StepDraft("timeseries_trend", "analytics.trend", series, "TrendOutput"),
        StepDraft("growth_rate", "analytics.growth", growth, "GrowthOutput"),
    ]


def _compare(ctx: TemplateContext) -> list[StepDraft]:
    if not ctx.metrics:
        return []
    return [
        StepDraft(
            "aggregate",
            "analytics.metric_query",
            {"metric": ctx.metrics[0], "dimensions": [d]},
            "MetricQueryOutput",
        )
        for d in ctx.dimensions
    ]


def _rank(ctx: TemplateContext) -> list[StepDraft]:
    if not ctx.metrics or not ctx.dimensions:
        return []
    args: dict[str, JsonValue] = {
        "metric": ctx.metrics[0],
        "dimension": ctx.dimensions[0],
        "top_n": 10,
    }
    return [StepDraft("rank", "analytics.rank", args, "RankOutput")]


def rca_dimensions(facts: DataFacts, config: AnalysisConfig) -> list[str]:
    """Dimensions a change can be decomposed by: categorical (non-time) dimensions with at most
    the RCA ``max_dimension_cardinality`` members, fewest first (the dashboard's choice in
    ``template_context``, before its KPI cap); the chat planner reuses it."""
    card = config.threshold("rca", "max_dimension_cardinality")
    limit = card.get("", 50) if isinstance(card, dict) else card
    return [d.name for d in facts.categorical_dimensions(limit)]


def _explain_change(ctx: TemplateContext) -> list[StepDraft]:
    t, metric = ctx.facts.time, ctx.series_metric
    if t is None or metric is None or not ctx.dimensions:
        return []
    dimensions: list[JsonValue] = list(ctx.dimensions)
    args: dict[str, JsonValue] = {
        "metric": metric,
        "current_start": str(period_start(t.end, t.planning_grain)),
        "current_end": str(t.end),
        "compare_to": "previous_period",
        "dimensions": dimensions,
        "grain": t.planning_grain,
        **_time_args(ctx),
    }
    return [
        StepDraft(
            "explain_change",
            "analytics.explain_change",
            args,
            "ExplainChangeOutput",
            ["schema", "numeric", "logic"],
        )
    ]


_SUM_OF_PRODUCT = re.compile(r"^sum\((measure\('[^']+'\)(?:\s*\*\s*measure\('[^']+'\))*)\)$")
_MEASURE = re.compile(r"measure\('([^']+)'\)")
_ML_AGG = {"sum": "sum", "avg": "mean", "count": "count"}


def ml_series(ctx: TemplateContext) -> dict[str, JsonValue] | None:
    """The series metric as ML tool arguments over stored columns: ``metric`` (+ ``multiply_by``)
    and ``agg``. ML tools read raw columns, not semantic expressions, so only a measure, a
    ``sum(measure(a) * measure(b) ...)`` metric or ``count()`` translates; else None."""
    name, model = ctx.series_metric, ctx.facts.model
    columns = {m.name: m.column for m in model.measures}
    measure = next((m for m in model.measures if m.name == name), None)
    if measure is not None and measure.default_agg in _ML_AGG:
        return {"metric": measure.column, "agg": _ML_AGG[measure.default_agg]}
    metric = next((m for m in model.metrics if m.name == name), None)
    expr = "" if metric is None else metric.expr.strip()
    if expr == "count()":
        return {"agg": "count"}
    match = _SUM_OF_PRODUCT.match(expr)
    names = _MEASURE.findall(match.group(1)) if match else []
    if not names or any(n not in columns for n in names):
        return None
    first, *rest = (columns[n] for n in names)
    return {"metric": first, "multiply_by": list(rest), "agg": "sum"}


def _forecast(ctx: TemplateContext) -> list[StepDraft]:
    t, series = ctx.facts.time, ml_series(ctx)
    if t is None or series is None or ctx.horizon is None:
        return []
    args: dict[str, JsonValue] = {
        **series,
        "time_column": t.column,
        "grain": t.planning_grain,
        "horizon": ctx.horizon,
        "seed": ctx.seed,
    }
    return [StepDraft("forecast", "ml.forecast", args, "ForecastOutput", ["schema", "model"])]


def _anomalies(ctx: TemplateContext) -> list[StepDraft]:
    t, series = ctx.facts.time, ml_series(ctx)
    if t is None or series is None or "metric" not in series:
        return []  # row-level anomalies over raw columns are not planned for the dashboard
    args: dict[str, JsonValue] = {
        **series,
        "time_column": t.column,
        "grain": t.planning_grain,
        "seed": ctx.seed,
    }
    return [
        StepDraft("anomaly_detection", "ml.anomalies", args, "AnomalyOutput", ["schema", "model"])
    ]


def _segments(ctx: TemplateContext) -> list[StepDraft]:
    customer = ctx.facts.entity("Customer")
    measures: list[JsonValue] = [m.column for m in ctx.facts.model.measures]
    if customer is None or "," in customer.key or not measures:
        return []
    args: dict[str, JsonValue] = {
        "entity_column": customer.key,
        "measures": measures,
        "seed": ctx.seed,
    }
    if ctx.facts.time is not None:
        args["time_column"] = ctx.facts.time.column
    return [StepDraft("segmentation", "ml.segments", args, "SegmentsOutput", ["schema", "model"])]


class PlanTemplateLibrary:
    TEMPLATES: ClassVar[Mapping[str, Template]] = {
        "overview": _overview,
        "metric_value": _metric_value,
        "trend": _trend,
        "compare": _compare,
        "rank": _rank,
        "explain_change": _explain_change,
        "forecast": _forecast,
        "anomalies": _anomalies,
        "segments": _segments,
    }

    def objectives(self) -> frozenset[str]:
        return frozenset(self.TEMPLATES)

    def steps(self, objective: str, ctx: TemplateContext) -> list[StepDraft]:
        template = self.TEMPLATES.get(objective)
        return [] if template is None else template(ctx)
