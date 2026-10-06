"""DefaultPlanner, templates and PlanValidator (TASK-ANALYSIS-004..006, TASK-INTEGRATION-003).

The analytics and ``ml.*`` tools are the real ones (their input models are what the
ToolExecutor enforces); nothing is executed, so they get no storage or database.
"""

from collections.abc import Callable
from typing import Any, cast

import pytest

from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.tools import analytics_tools
from app.ml.anomaly_tool import AnomalyTool
from app.ml.forecast_tool import ForecastTool
from app.ml.segments_tool import SegmentsTool
from app.planner.capabilities import CapabilityEvaluator
from app.planner.default_planner import DefaultPlanner
from app.planner.domain import AnalysisPlan, AnalysisStep, CapabilityReport, Unavailability
from app.planner.templates import PlanTemplateLibrary
from app.planner.validator import PlanError, PlanValidator
from app.tools.domain import Tool
from app.tools.registry import ToolRegistry

from .conftest import Understood

Get = Callable[[str], Understood]
ML_TOOLS = ("ml.forecast", "ml.anomalies", "ml.segments")


def _registry(*, ml: bool) -> ToolRegistry:
    tools: list[Tool] = analytics_tools(cast(Any, None), AnalyticalEngine())
    if ml:
        tools += [
            cls(cast(Any, None), cast(Any, None))
            for cls in (ForecastTool, AnomalyTool, SegmentsTool)
        ]
    return ToolRegistry(tools)


ANALYTICS, FULL = _registry(ml=False), _registry(ml=True)


def _plan(fixture: Understood, registry: ToolRegistry) -> tuple[AnalysisPlan, dict[str, str]]:
    report = CapabilityEvaluator().evaluate(fixture.facts)
    planned = DefaultPlanner(registry).plan(fixture.facts, report)
    PlanValidator(registry).validate(planned.plan, report)
    return planned.plan, planned.deferred


@pytest.mark.parametrize("name", ["retail", "saas", "agriculture", "insufficient", "ambiguous"])
def test_default_plans_validate_with_the_real_analytics_tools(understood: Get, name: str) -> None:
    plan, deferred = _plan(understood(name), ANALYTICS)
    assert plan.intent.source == "system_default"
    assert {s.tool for s in plan.steps} <= {
        "analytics.metric_query",
        "analytics.trend",
        "analytics.growth",
        "analytics.explain_change",
    }
    assert set(deferred.values()) <= {f"tool {t} is not registered yet" for t in ML_TOOLS}
    AnalysisPlan.model_validate_json(plan.model_dump_json())  # schema round trip


def test_retail_plan_with_ml_tools(understood: Get) -> None:
    plan, deferred = _plan(understood("retail"), FULL)
    assert deferred == {}
    tools = [s.tool for s in plan.steps]
    assert tools[:5] == ["analytics.metric_query"] * 5  # KPI cards first
    assert tools[-3:] == list(ML_TOOLS)
    forecast = next(s for s in plan.steps if s.tool == "ml.forecast")
    assert forecast.args["grain"] == "month"
    assert forecast.args["horizon"] == 3
    # Revenue = sum(Quantity * Unit Price) -> the ML tools' stored-column form
    assert (forecast.args["metric"], forecast.args["multiply_by"]) == ("quantity", ["unit_price"])
    assert "task" not in forecast.args
    assert "dataset_version_id" not in forecast.args
    segments = next(s for s in plan.steps if s.tool == "ml.segments")
    assert segments.args["entity_column"] == "customer_id"
    assert [s.id for s in plan.steps] == [f"S{i}" for i in range(1, len(plan.steps) + 1)]


def test_insufficient_plan_omits_forecast_and_carries_reasons(understood: Get) -> None:
    plan, deferred = _plan(understood("insufficient"), FULL)
    assert "ml.forecast" not in {s.tool for s in plan.steps}
    assert "forecast" not in deferred  # unavailable, not deferred
    reasons = {(u.analysis, u.reason_code.value) for u in plan.unavailable}
    assert ("forecast", "INSUFFICIENT_HISTORY") in reasons
    assert ("customer_segmentation", "NO_CUSTOMER_IDENTIFIER") in reasons


def test_same_input_same_plan(understood: Get) -> None:
    fixture = understood("saas")
    assert _plan(fixture, FULL) == _plan(fixture, FULL)


def test_templates_cover_every_dashboard_objective() -> None:
    library = PlanTemplateLibrary()
    assert {"overview", "trend", "compare", "rank", "explain_change", "forecast"} <= set(
        library.objectives()
    )


def _base(understood: Get) -> tuple[AnalysisPlan, CapabilityReport]:
    fixture = understood("retail")
    report = CapabilityEvaluator().evaluate(fixture.facts)
    return DefaultPlanner(FULL).plan(fixture.facts, report).plan, report


def _step(**changes: Any) -> AnalysisStep:
    base = {
        "id": "X1",
        "type": "aggregate",
        "tool": "analytics.metric_query",
        "args": {"metric": "Revenue"},
        "expected_output": "MetricQueryOutput",
    }
    return AnalysisStep.model_validate(base | changes)


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        (_step(tool="analytics.nope"), "unknown tool 'analytics.nope'"),
        (_step(args={"metric": 5}), "invalid arguments for analytics.metric_query: metric"),
        (_step(args={"metric": "Revenue", "extra": 1}), "invalid arguments"),
        (_step(type="classification_fit"), "unknown step type"),
    ],
)
def test_validator_rejects_bad_steps(understood: Get, step: AnalysisStep, reason: str) -> None:
    plan, report = _base(understood)
    bad = plan.model_copy(update={"steps": [*plan.steps[:3], step]})
    with pytest.raises(PlanError) as err:
        PlanValidator(FULL).validate(bad, report)
    assert any(reason in r for r in err.value.reasons), err.value.reasons


def test_validator_rejects_unavailable_analysis_and_contradictions(understood: Get) -> None:
    fixture = understood("insufficient")
    report = CapabilityEvaluator().evaluate(fixture.facts)
    plan = DefaultPlanner(FULL).plan(fixture.facts, report).plan
    forecast = _step(type="forecast", tool="ml.forecast", args={})
    lying = Unavailability(analysis="kpi", reason_code="NO_MEASURE", message="Insufficient data")
    bad = plan.model_copy(update={"steps": [forecast], "unavailable": [lying]})
    with pytest.raises(PlanError) as err:
        PlanValidator(FULL).validate(bad, report)
    text = " | ".join(err.value.reasons)
    assert "analysis 'forecast' is not available" in text
    assert "contradicts the capability report" in text
    assert "invalid arguments for ml.forecast" in text


def test_validator_rejects_cycles_budgets_and_version_mismatch(understood: Get) -> None:
    plan, report = _base(understood)
    a = _step(id="A", depends_on=["B"])
    b = _step(id="B", depends_on=["A"])
    cyclic = AnalysisPlan.model_construct(**{**dict(plan), "steps": [a, b]})
    with pytest.raises(PlanError) as err:
        PlanValidator(FULL).validate(cyclic, report)
    assert "steps contain a dependency cycle" in err.value.reasons
    many = [_step(id=f"K{i}") for i in range(9)]  # metric_query budget is 8 per plan
    with pytest.raises(PlanError) as err:
        PlanValidator(FULL).validate(plan.model_copy(update={"steps": many}), report)
    assert err.value.reasons == ["tool 'analytics.metric_query' is called 9 times; its budget is 8"]
    other = report.model_copy(update={"semantic_model_version": 2})
    with pytest.raises(PlanError) as err:
        PlanValidator(FULL).validate(plan, other)
    assert "different dataset/semantic model versions" in err.value.reasons[0]


def test_plan_error_is_a_422_with_reasons() -> None:
    error = PlanError(["x"])
    assert (error.code, error.http_status) == ("VALIDATION_ERROR", 422)
    assert error.details == {"reasons": ["x"]}
