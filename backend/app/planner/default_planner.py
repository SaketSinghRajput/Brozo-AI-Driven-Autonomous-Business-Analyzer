"""DefaultPlanner (TASK-ANALYSIS-005; design 17, TRD 14.4: "no LLM in dashboard planning").

Builds the dashboard plan of a dataset version from its semantic model, data facts and
capability report: every *available* objective, in a fixed priority order, expanded by the
template library. Same input -> same plan (deterministic id, order and arguments).
An objective is left out when its analysis is unavailable (the report's ``Unavailability``
entries are carried into ``plan.unavailable``), when its template has nothing to work with (e.g.
no dimension to compare by), or - reported in ``deferred`` - when one of its tools is not
registered yet (the ``ml.*`` tools) or the step / per-tool call budget is used up. The plan is not
executed here: ``PlanValidator`` then ``PlanExecutor`` (TASK-ANALYSIS-007) take it from there.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass

from app.core.errors import ToolError
from app.planner.config import AnalysisConfig, default_analysis_config
from app.planner.domain import AnalysisIntent, AnalysisPlan, AnalysisStep, CapabilityReport
from app.planner.facts import DataFacts
from app.planner.templates import PlanTemplateLibrary, StepDraft, TemplateContext
from app.tools.registry import ToolRegistry

# (objective, capability it needs), in dashboard priority order.
OBJECTIVES = (
    ("overview", "kpi"),
    ("trend", "trend"),
    ("compare", "kpi"),
    ("explain_change", "rca"),
    ("forecast", "forecast"),
    ("anomalies", "anomaly_detection"),
    ("segments", "customer_segmentation"),
)
_SUMMABLE = frozenset({"additive", "semi_additive"})


@dataclass(frozen=True)
class DefaultPlan:
    plan: AnalysisPlan
    deferred: dict[str, str]  # objective -> why it is not in the plan although the data allows it


def plan_id(facts: DataFacts, report: CapabilityReport, config_version: str) -> str:
    key = (
        f"{facts.dataset_version_id}:{facts.model.version}:{report.rules_version}:{config_version}"
    )
    return f"pl_{hashlib.sha256(key.encode()).hexdigest()[:26]}"


def _number(value: float | dict[str, float], key: str, default: float) -> float:
    return value.get(key, default) if isinstance(value, dict) else value


def template_context(facts: DataFacts, config: AnalysisConfig) -> TemplateContext:
    limits = config.planner
    model = facts.model
    metrics = [m.name for m in model.metrics] + [m.name for m in model.measures]
    summable = [m.name for m in model.metrics if m.additivity in _SUMMABLE]
    summable += [m.name for m in model.measures if m.additivity in _SUMMABLE]
    max_card = _number(config.threshold("rca", "max_dimension_cardinality"), "", 50)
    dims = facts.categorical_dimensions(max_card)
    horizon = None
    if facts.time is not None:
        grain = facts.time.planning_grain
        horizon = int(_number(config.threshold("forecast", "horizon"), grain, 1))
    return TemplateContext(
        facts=facts,
        metrics=metrics[: limits.max_kpis],
        series_metric=summable[0] if summable else None,
        dimensions=[d.name for d in dims[: limits.max_dimensions]],
        seed=limits.seed,
        horizon=horizon,
    )


class DefaultPlanner:
    def __init__(
        self,
        registry: ToolRegistry,
        templates: PlanTemplateLibrary | None = None,
        config: AnalysisConfig | None = None,
    ) -> None:
        self._registry = registry
        self._templates = templates or PlanTemplateLibrary()
        self._config = config or default_analysis_config()

    def plan(self, facts: DataFacts, report: CapabilityReport) -> DefaultPlan:
        ctx = template_context(facts, self._config)
        available = set(report.available)
        drafts: list[StepDraft] = []
        deferred: dict[str, str] = {}
        for objective, analysis in OBJECTIVES:
            if analysis not in available:
                continue
            steps = self._templates.steps(objective, ctx)
            missing = sorted({s.tool for s in steps if self._budget(s.tool) is None})
            calls = Counter(s.tool for s in [*drafts, *steps])
            if missing:
                deferred[objective] = f"tool {missing[0]} is not registered yet"
            elif len(drafts) + len(steps) > self._config.planner.max_steps or any(
                n > (self._budget(tool) or 0) for tool, n in calls.items()
            ):
                deferred[objective] = "the plan's step or tool-call budget is used up"
            else:
                drafts += steps
        intent = AnalysisIntent(objective="overview", metrics=ctx.metrics, source="system_default")
        plan = AnalysisPlan(
            plan_id=plan_id(facts, report, self._config.version),
            version=1,
            dataset_version_id=facts.dataset_version_id,
            semantic_model_version=facts.model.version,
            intent=intent,
            steps=[
                AnalysisStep(
                    id=f"S{i}",
                    type=d.type,
                    tool=d.tool,
                    args=d.args,
                    expected_output=d.expected_output,
                    validation=d.validation,
                )
                for i, d in enumerate(drafts, start=1)
            ],
            unavailable=report.unavailable,
        )
        return DefaultPlan(plan, deferred)

    def _budget(self, tool: str) -> int | None:
        """The tool's calls-per-plan budget; None when the tool is not registered."""
        try:
            return self._registry.get(tool).meta.max_calls_per_turn
        except ToolError:
            return None
