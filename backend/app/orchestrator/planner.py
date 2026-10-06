"""QuestionPlanner: resolved intent → template → validated AnalysisPlan (TASK-AGENT-003).

The LLM never emits plan steps: templates below map each P1 objective to fixed tool calls whose
arguments are filled from the (already name-resolved) ``AnalysisIntent``. ``PlanValidator`` is
the minimal port this module needs; ``RegistryPlanValidator`` implements it against the tool
registry until app/planner ships its PlanValidator and templates, which can replace both.

Forecast and anomaly questions reuse the dashboard planner's capability report (the specific
"Insufficient data" reason when unavailable) and its templates (``app.planner.templates``,
including the semantic-metric -> stored-column mapping), on the asked metric. They need the
version's ``DataFacts``; without them the question is refused.

ponytail: comparisons always use the previous period; ML runs over the whole history with the
configured horizon (a question with filters is refused rather than answered unfiltered);
segments/predict still refuse.
"""

import json
from dataclasses import replace
from typing import Protocol

from pydantic import JsonValue, ValidationError

from app.analytics.sql.resolver import SemanticResolver
from app.core.errors import SchemaError, ToolError
from app.core.ids import IdGenerator
from app.data.semantic.domain import SemanticModel
from app.orchestrator.domain import Contract, Refusal
from app.planner.capabilities import CapabilityEvaluator
from app.planner.config import default_analysis_config
from app.planner.default_planner import template_context
from app.planner.domain import AnalysisIntent, AnalysisPlan, AnalysisStep
from app.planner.facts import DataFacts
from app.planner.templates import PlanTemplateLibrary, rca_dimensions
from app.tools.domain import ToolPermission
from app.tools.registry import ToolRegistry

Args = dict[str, JsonValue]
RANGE_OBJECTIVES = frozenset({"compare", "trend", "explain_change"})
ML_OBJECTIVES = {
    "forecast": "Forecasting",
    "anomalies": "Anomaly detection",
    "segments": "Segmentation",
    "predict": "Prediction",
}
# objective -> (capability-report analysis, label); planned from app.planner templates
ML_PLANNED = {
    "forecast": ("forecast", "Forecasting"),
    "anomalies": ("anomaly_detection", "Anomaly detection"),
}
MAX_OVERVIEW_METRICS = 3
MAX_RCA_DIMENSIONS = 8


class Clarification(Contract):
    message: str


type PlanOutcome = AnalysisPlan | Refusal | Clarification


class PlanValidator(Protocol):
    def validate(self, plan: AnalysisPlan, permissions: frozenset[ToolPermission]) -> list[str]: ...


class RegistryPlanValidator:
    """Each step names a registered, permitted tool whose strict input model accepts the args."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def validate(self, plan: AnalysisPlan, permissions: frozenset[ToolPermission]) -> list[str]:
        problems = []
        for step in plan.steps:
            try:
                tool = self._registry.get(step.tool)
            except ToolError:
                problems.append(f"{step.id}: tool is not registered")
                continue
            if tool.meta.permission not in permissions:
                problems.append(f"{step.id}: tool is not permitted in this turn")
            try:
                tool.input_model.model_validate_json(json.dumps(step.args), strict=True)
            except ValidationError:
                problems.append(f"{step.id}: arguments do not match the tool input schema")
        return problems


class QuestionPlanner:
    def __init__(
        self, registry: ToolRegistry, ids: IdGenerator, validator: PlanValidator | None = None
    ) -> None:
        self._validator = validator or RegistryPlanValidator(registry)
        self._ids = ids
        self._capabilities = CapabilityEvaluator()
        self._templates = PlanTemplateLibrary()
        self._config = default_analysis_config()

    def plan(
        self,
        intent: AnalysisIntent,
        model: SemanticModel,
        permissions: frozenset[ToolPermission],
        facts: DataFacts | None = None,
    ) -> PlanOutcome:
        if intent.objective in ML_PLANNED:
            planned = self._ml_steps(intent, facts)
            if isinstance(planned, Refusal):
                return planned
            steps = planned
        elif intent.objective in ML_OBJECTIVES:
            label = ML_OBJECTIVES[intent.objective]
            return Refusal(code="NOT_AVAILABLE", message=f"{label} is not available yet.")
        else:
            time = intent.time
            if intent.objective in RANGE_OBJECTIVES and not (time and time.start and time.end):
                return Clarification(message="Which period should I analyse? Give a start and end.")
            try:
                candidates = rca_dimensions(facts, self._config) if facts else None
                steps = _steps(intent, SemanticResolver(model), candidates)
            except SchemaError as exc:
                message = f"Insufficient data: {exc.message}"
                return Refusal(code="INSUFFICIENT_DATA", message=message)
        plan = AnalysisPlan(
            plan_id=self._ids.new("pl"),
            version=1,
            dataset_version_id=model.dataset_version_id,
            semantic_model_version=model.version,
            intent=intent,
            steps=steps,
        )
        problems = self._validator.validate(plan, permissions)
        if problems:
            return Refusal(
                code="INSUFFICIENT_DATA",
                message="Insufficient data: no valid analysis plan for this question.",
                suggestions=problems[:3],
            )
        return plan

    def _ml_steps(
        self, intent: AnalysisIntent, facts: DataFacts | None
    ) -> list[AnalysisStep] | Refusal:
        analysis, label = ML_PLANNED[intent.objective]
        if facts is None:
            message = f"{label} needs the dataset's profile and understanding, which are missing."
            return Refusal(code="NOT_AVAILABLE", message=message)
        if intent.filters:
            message = f"{label} of a filtered subset is not available yet; ask without filters."
            return Refusal(code="NOT_AVAILABLE", message=message)
        report = self._capabilities.evaluate(facts)
        if analysis not in report.available:
            reasons = [u for u in report.unavailable if u.analysis == analysis]
            message = reasons[0].message if reasons else f"Insufficient data: {label} rules failed."
            hints = [u.would_enable for u in reasons if u.would_enable]
            return Refusal(code="INSUFFICIENT_DATA", message=message, suggestions=hints[:3])
        ctx = template_context(facts, self._config)
        ctx = replace(ctx, series_metric=intent.metrics[0])
        drafts = self._templates.steps(intent.objective, ctx)
        if not drafts:
            message = (
                f"Insufficient data: {label} needs {intent.metrics[0]} as a measure or a sum of "
                "measures over stored columns, and a time dimension."
            )
            return Refusal(code="INSUFFICIENT_DATA", message=message)
        return [
            AnalysisStep(
                id=f"S{i}",
                type=d.type,
                tool=d.tool,
                args=d.args,
                expected_output=d.expected_output,
                validation=d.validation,
            )
            for i, d in enumerate(drafts, start=1)
        ]


def _step(n: int, tool: str, args: Args, kind: str, expected: str) -> AnalysisStep:
    return AnalysisStep(id=f"S{n}", type=kind, tool=tool, args=args, expected_output=expected)


def _steps(
    intent: AnalysisIntent, resolver: SemanticResolver, candidates: list[str] | None = None
) -> list[AnalysisStep]:
    """``candidates``: the dimensions an unspecified RCA decomposes by (``rca_dimensions`` when
    the data facts are known; else every category/geo/boolean dimension of the model)."""
    metric = intent.metrics[0]
    filters: list[JsonValue] = [f.model_dump(mode="json") for f in intent.filters]
    time = intent.time
    start = time.start.isoformat() if time and time.start else None
    end = time.end.isoformat() if time and time.end else None
    grain = time.grain if time else "month"
    ranged: Args = {"start": start, "end": end} if start and end else {}
    # Not a grouping: a dimension pinned by a filter (time dimensions: dropped by the post-check).
    fixed = {f.dimension for f in intent.filters if f.op in ("eq", "between")}
    times = [d.name for d in resolver.model.dimensions if d.type == "time"]
    dimensions = [d for d in intent.dimensions if d not in fixed and d not in times]
    # B1: the period's date is the intent's time dimension, else the model's first (the
    # understanding ranks the preferred one first); named explicitly when it matters.
    tdim = intent.time_dimension or (times[0] if times else None)
    explicit = tdim is not None and (intent.time_dimension is not None or len(times) > 1)
    named: Args = {"time_dimension": tdim} if explicit else {}
    compare_to = intent.compare_to or "previous_period"  # B4
    yoy: Args = {"compare_to": compare_to} if compare_to != "previous_period" else {}
    match intent.objective:
        case "rank" | "metric_value" if dimensions:
            args: Args = {"metric": metric, "dimension": dimensions[0], "filters": filters}
            rank_args = {**args, **ranged, **(named if ranged else {})}
            return [_step(1, "analytics.rank", rank_args, "rank", "ranking")]
        case "compare":
            query: Args = {
                "metric": metric,
                "filters": filters,
                "time": {"grain": grain, "start": start, "end": end},
                "compare_to": compare_to,
                **named,
            }
            growth: Args = {"metric": metric, "filters": filters, **ranged, **named, **yoy}
            return [
                _step(1, "analytics.metric_query", query, "aggregate", "current value"),
                _step(2, "analytics.growth", growth, "growth_rate", "percent change"),
            ]
        case "trend":
            trend: Args = {"metric": metric, "grain": grain, "filters": filters, **ranged, **named}
            return [_step(1, "analytics.trend", trend, "trend", "Sen's slope per period")]
        case "explain_change":
            # RCA-FIX: the intent keeps only dimensions the question names (intent.post_check);
            # none named -> every candidate, minus the filtered ones, up to the tool's limit.
            filtered = {f.dimension for f in intent.filters}
            pool = candidates or [
                d.name
                for d in resolver.model.dimensions
                if d.type in ("category", "geo", "boolean")
            ]
            dims = intent.dimensions or [d for d in pool if d not in filtered and d not in times]
            rca: Args = {
                "metric": metric,
                "current_start": start,
                "current_end": end,
                "filters": filters,
                "dimensions": list(dims[:MAX_RCA_DIMENSIONS]),
                **named,
                **yoy,
            }
            growth = {"metric": metric, "filters": filters, **ranged, **named, **yoy}
            return [
                _step(1, "analytics.explain_change", rca, "root_cause", "change + factors"),
                _step(2, "analytics.growth", growth, "growth_rate", "percent change"),
            ]
        case _:  # metric_value / overview: one ungrouped total per metric over the range
            if start and end:
                name = tdim or resolver.time_dimension(None).name
                filters = [*filters, {"dimension": name, "op": "between", "value": [start, end]}]
            metrics = intent.metrics[:MAX_OVERVIEW_METRICS]
            tool = "analytics.metric_query"
            return [
                _step(i, tool, {"metric": m, "filters": filters}, "aggregate", m)
                for i, m in enumerate(metrics, start=1)
            ]
