"""F1 / B1 / B4: deterministic intent normalization and time-dimension / comparison handling.

F1: models put the period into ``filters`` instead of start/end (BENCH-002, every model tried);
the post-check lifts a filter on a time dimension into start/end with the right grain. Time
dimensions in ``dimensions`` are never groupings: dropped in one place (the post-check).
B1: with two time dimensions (SaaS Start Date / End Date) every dated question used to fail;
the planner now names the time dimension (the intent's, else the model's first).
B4: ``compare_to`` (previous_period | previous_year) reaches the analytics tools.
"""

from datetime import date
from typing import Any

import pytest

from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.tools import analytics_tools
from app.core.ids import SequentialIds
from app.data.semantic.domain import SemanticModel
from app.orchestrator.intent import IntentOutput, post_check
from app.orchestrator.planner import QuestionPlanner
from app.planner.domain import AnalysisIntent, AnalysisPlan
from app.tools.permissions import Role, TurnProfile, effective_permissions
from app.tools.registry import ToolRegistry
from tests.unit.analytics.conftest import semantic_model, small_orders
from tests.unit.orchestrator.fakes import MemorySource

CHAT = effective_permissions(Role.ANALYST, TurnProfile.CHAT_DEFAULT)
BASE_DIMS = semantic_model().model_dump()["dimensions"]
TWO_TIMES = semantic_model(
    dimensions=[
        *BASE_DIMS,
        {"name": "Ship Date", "column": "ship_date", "type": "time", "grain": "day"},
    ]
)
YEARLY = semantic_model(
    dimensions=[
        *BASE_DIMS[:2],
        {"name": "Crop Year", "column": "year", "type": "time", "grain": "year"},
    ]
)
AUG = {"start": "2026-08-01", "end": "2026-08-31"}


def _out(**fields: Any) -> IntentOutput:
    base = {"route": "structured", "objective": "metric_value"}
    return IntentOutput.model_validate({**base, **fields})


def _intent(out: IntentOutput, model: SemanticModel = TWO_TIMES) -> AnalysisIntent:
    resolved = post_check(out, "q", model)
    assert resolved.intent is not None, resolved
    return resolved.intent


@pytest.mark.parametrize(
    ("flt", "start", "end", "grain"),
    [
        ({"op": "between", "value": ["2026-08-01", "2026-08-31"]}, *AUG.values(), "month"),
        ({"op": "eq", "value": "2026-08"}, "2026-08-01", "2026-08-31", "month"),
        ({"op": "eq", "value": "2026-08-14"}, "2026-08-14", "2026-08-14", "day"),
        ({"op": "eq", "value": 2025}, "2025-01-01", "2025-12-31", "year"),
        ({"op": "eq", "value": "2025"}, "2025-01-01", "2025-12-31", "year"),
        ({"op": "between", "value": [2023, 2024]}, "2023-01-01", "2024-12-31", "year"),
        ({"op": "gte", "value": "2026-07-01"}, "2026-07-01", None, "month"),
    ],
)  # fmt: skip
def test_a_period_filter_is_lifted_into_start_and_end(
    flt: dict[str, Any], start: str, end: str | None, grain: str
) -> None:
    region = {"dimension": "Region", "op": "eq", "value": "North"}
    period = {"dimension": "order date", **flt}
    intent = _intent(_out(metrics=["Revenue"], filters=[region, period]))
    assert [f.dimension for f in intent.filters] == ["Region"]  # the member filter stays
    assert intent.time is not None
    assert intent.time.start == date.fromisoformat(start)
    assert intent.time.end == (date.fromisoformat(end) if end else None)
    assert intent.time.grain == grain
    assert intent.time_dimension == "Order Date"


def test_explicit_start_end_win_and_the_period_filter_is_still_removed() -> None:
    period = {"dimension": "Order Date", "op": "eq", "value": "2026-07"}
    intent = _intent(_out(metrics=["Revenue"], filters=[period], **AUG))
    assert intent.filters == []
    assert intent.time is not None
    assert (intent.time.start, intent.time.end) == (date(2026, 8, 1), date(2026, 8, 31))


def test_time_dimensions_are_never_groupings() -> None:
    intent = _intent(_out(metrics=["Revenue"], dimensions=["Region", "Ship Date"]))
    assert intent.dimensions == ["Region"]
    assert intent.time_dimension == "Ship Date"


def test_previous_year_comparison_maps_to_the_analytics_vocabulary() -> None:
    out = _out(objective="compare", metrics=["Revenue"], compare_to="previous_year", **AUG)
    assert _intent(out).compare_to == "same_period_last_year"


def _plan(intent: AnalysisIntent, model: SemanticModel) -> AnalysisPlan:
    source = MemorySource(model, small_orders())
    registry = ToolRegistry(analytics_tools(source, AnalyticalEngine()))
    plan = QuestionPlanner(registry, SequentialIds()).plan(intent, model, CHAT)
    assert isinstance(plan, AnalysisPlan), plan
    return plan


@pytest.mark.parametrize("objective", ["metric_value", "compare", "trend", "explain_change"])
def test_two_time_dimensions_plan_with_a_named_time_dimension(objective: str) -> None:
    intent = _intent(_out(objective=objective, metrics=["Revenue"], **AUG))
    for step in _plan(intent, TWO_TIMES).steps:
        named = step.args.get("time_dimension")
        filters = step.args.get("filters")
        listed = filters if isinstance(filters, list) else []
        dated = [f for f in listed if isinstance(f, dict) and f["dimension"] == "Order Date"]
        assert named == "Order Date" or dated, step  # the model's first time dimension


def test_the_intent_time_dimension_is_used() -> None:
    out = _out(objective="trend", metrics=["Revenue"], time_dimension="ship date", **AUG)
    assert _plan(_intent(out), TWO_TIMES).steps[0].args["time_dimension"] == "Ship Date"


def test_previous_year_reaches_the_tools() -> None:
    out = _out(objective="compare", metrics=["Revenue"], compare_to="previous_year", **AUG)
    query, growth = _plan(_intent(out, semantic_model()), semantic_model()).steps
    assert query.args["compare_to"] == "same_period_last_year"
    assert growth.args["compare_to"] == "same_period_last_year"


def test_year_filters_on_an_integer_year_dimension() -> None:
    year = {"dimension": "Crop Year", "op": "eq", "value": 2025}
    intent = _intent(_out(metrics=["Revenue"], filters=[year]), YEARLY)
    assert intent.time is not None
    assert (intent.time.start, intent.time.end) == (date(2025, 1, 1), date(2025, 12, 31))
    assert intent.time.grain == "year"
