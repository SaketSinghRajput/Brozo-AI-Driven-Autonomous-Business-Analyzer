"""PlanValidator (TASK-ANALYSIS-006; design 17-18, TRD 14.4).

A plan runs only as a ``ValidatedPlan`` (the PlanExecutor's input, TASK-ANALYSIS-007). Rejected:
- versions that do not match the capability report it was planned against;
- a step whose analysis is unavailable, or whose step type maps to no known analysis;
- an ``unavailable`` entry for an analysis the report says is available (contradiction);
- unknown tools, and arguments that fail the tool's input model in strict JSON mode (the same
  parse the ToolExecutor applies, so a validated plan never fails on argument shape);
- dependency cycles or unknown dependencies (also enforced by the contract; re-checked for plans
  built without validation);
- budgets: more steps than ``planner.max_steps``, or more calls to one tool than its
  ``max_calls_per_turn``.
All reasons are collected and raised together in one ``PlanError`` (422 VALIDATION_ERROR).
"""

import json
from collections import Counter
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from pydantic import ValidationError

from app.core.errors import AppValidationError, ToolError
from app.planner.config import PlannerLimits, default_analysis_config
from app.planner.domain import AnalysisPlan, AnalysisStep, CapabilityReport
from app.tools.registry import ToolRegistry

STEP_ANALYSIS = {
    "aggregate": "kpi",
    "rank": "kpi",
    "timeseries_trend": "trend",
    "growth_rate": "trend",
    "explain_change": "rca",
    "forecast": "forecast",
    "anomaly_detection": "anomaly_detection",
    "segmentation": "customer_segmentation",
}


class PlanError(AppValidationError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("The analysis plan was rejected.", details={"reasons": reasons})
        self.reasons = reasons


@dataclass(frozen=True)
class ValidatedPlan:
    plan: AnalysisPlan
    report: CapabilityReport


class PlanValidator:
    def __init__(self, registry: ToolRegistry, limits: PlannerLimits | None = None) -> None:
        self._registry = registry
        self._limits = limits or default_analysis_config().planner

    def validate(self, plan: AnalysisPlan, report: CapabilityReport) -> ValidatedPlan:
        reasons = self._versions(plan, report) + self._graph(plan)
        budget = self._limits.max_steps
        if len(plan.steps) > budget:
            reasons.append(f"plan has {len(plan.steps)} steps; the budget is {budget}")
        available = set(report.available)
        reasons += [
            f"unavailable entry for {u.analysis!r} contradicts the capability report"
            for u in plan.unavailable
            if u.analysis in available
        ]
        for step in plan.steps:
            reasons += self._step(step, available)
        for tool, calls in sorted(Counter(s.tool for s in plan.steps).items()):
            try:
                limit = self._registry.get(tool).meta.max_calls_per_turn
            except ToolError:
                continue  # reported per step
            if calls > limit:
                reasons.append(f"tool {tool!r} is called {calls} times; its budget is {limit}")
        if reasons:
            raise PlanError(reasons)
        return ValidatedPlan(plan, report)

    @staticmethod
    def _versions(plan: AnalysisPlan, report: CapabilityReport) -> list[str]:
        if (plan.dataset_version_id, plan.semantic_model_version) == (
            report.dataset_version_id,
            report.semantic_model_version,
        ):
            return []
        return ["plan and capability report refer to different dataset/semantic model versions"]

    @staticmethod
    def _graph(plan: AnalysisPlan) -> list[str]:
        ids = {s.id for s in plan.steps}
        reasons = [
            f"step {s.id}: unknown dependency {d!r}"
            for s in plan.steps
            for d in s.depends_on
            if d not in ids
        ]
        try:
            TopologicalSorter({s.id: s.depends_on for s in plan.steps}).prepare()
        except CycleError:
            reasons.append("steps contain a dependency cycle")
        return reasons

    def _step(self, step: AnalysisStep, available: set[str]) -> list[str]:
        analysis = STEP_ANALYSIS.get(step.type)
        if analysis is None:
            return [f"step {step.id}: unknown step type {step.type!r}"]
        reasons = []
        if analysis not in available:
            reasons.append(f"step {step.id}: analysis {analysis!r} is not available for this data")
        try:
            tool = self._registry.get(step.tool)
        except ToolError:
            return [*reasons, f"step {step.id}: unknown tool {step.tool!r}"]
        try:
            tool.input_model.model_validate_json(json.dumps(step.args), strict=True)
        except ValidationError as exc:
            fields = ", ".join(".".join(map(str, e["loc"])) or "<root>" for e in exc.errors())
            reasons.append(f"step {step.id}: invalid arguments for {step.tool}: {fields}")
        return reasons
