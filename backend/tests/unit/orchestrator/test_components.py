"""Unit tests for the turn components: state machine, intent post-checks, planner templates,
composer helpers and claim extraction (TASK-AGENT-001/002/003/005/006)."""

import asyncio
from datetime import date
from typing import Any

import pytest

from app.analytics.services.growth import growth
from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.tools import analytics_tools
from app.core.ids import SequentialIds
from app.orchestrator.composer import display_format, extract_claims, facts_from, format_value
from app.orchestrator.domain import Refusal, TurnLimits, TurnState
from app.orchestrator.intent import NO_DOCUMENTS, IntentOutput, post_check
from app.orchestrator.planner import Clarification, PlanOutcome, QuestionPlanner
from app.orchestrator.state import IllegalTransition, TurnBudgetExceeded, TurnStateMachine
from app.orchestrator.tool_loop import ExecutedCall, args_summary
from app.planner.domain import AnalysisIntent, AnalysisPlan, TimeSpec
from app.tools.domain import ToolPermission, ToolResult
from app.tools.permissions import Role, TurnProfile, effective_permissions
from app.tools.registry import ToolRegistry
from app.validator.domain import AnswerSegment, CaveatSegment, NumberSegment, TextSegment
from tests.unit.analytics.conftest import semantic_model, small_orders
from tests.unit.orchestrator.fakes import MemorySource
from tests.unit.validator import factories

S = TurnState
MODEL = semantic_model()
CHAT = effective_permissions(Role.ANALYST, TurnProfile.CHAT_DEFAULT)
REGISTRY = ToolRegistry(analytics_tools(MemorySource(MODEL, small_orders()), AnalyticalEngine()))
AUGUST = TimeSpec(grain="month", start=date(2026, 8, 1), end=date(2026, 8, 31))


# --- TurnStateMachine ---------------------------------------------------------------------


def test_happy_path_and_illegal_transitions() -> None:
    m = TurnStateMachine(TurnLimits())
    for state in (S.CLASSIFYING, S.PLANNING, S.EXECUTING, S.WAITING_FOR_TOOL, S.EXECUTING):
        m.to(state)
    with pytest.raises(IllegalTransition):
        m.to(S.COMPLETED)  # must compose and validate first
    for state in (S.COMPOSING, S.VALIDATING, S.COMPLETED):
        m.to(state)
    assert m.terminal
    with pytest.raises(IllegalTransition):
        m.to(S.FAILED)  # terminal states stay terminal


def test_retry_budget_and_failure_path() -> None:
    m = TurnStateMachine(TurnLimits(max_retries=1))
    for state in (S.CLASSIFYING, S.PLANNING, S.EXECUTING, S.COMPOSING, S.VALIDATING, S.RETRYING):
        m.to(state)
    m.to(S.COMPOSING)
    m.to(S.VALIDATING)
    with pytest.raises(IllegalTransition, match="retry budget"):
        m.to(S.RETRYING)
    m.to(S.FAILED)
    m.to(S.RECOVERED)
    assert m.history[-2:] == [S.FAILED, S.RECOVERED]


def test_step_token_and_wall_clock_budgets() -> None:
    m = TurnStateMachine(TurnLimits(max_steps=2))
    m.to(S.CLASSIFYING)
    m.to(S.PLANNING)
    with pytest.raises(TurnBudgetExceeded, match="step"):
        m.to(S.EXECUTING)

    m = TurnStateMachine(TurnLimits(max_llm_tokens=100))
    m.charge_tokens(100)
    with pytest.raises(TurnBudgetExceeded, match="token"):
        m.charge_tokens(1)

    now = [0.0]
    m = TurnStateMachine(TurnLimits(wall_clock_timeout_s=5), now=lambda: now[0])
    m.to(S.CLASSIFYING)
    now[0] = 6.0
    with pytest.raises(TurnBudgetExceeded, match="wall-clock"):
        m.to(S.PLANNING)


def test_bounded_cuts_a_hanging_call() -> None:
    m = TurnStateMachine(TurnLimits(wall_clock_timeout_s=0.05))
    with pytest.raises(TurnBudgetExceeded):
        asyncio.run(m.bounded(asyncio.sleep(5)))


def test_loop_detector_refuses_identical_calls() -> None:
    m = TurnStateMachine(TurnLimits())
    assert m.record_call("analytics.rank", {"metric": "Revenue", "dimension": "Region"})
    assert m.record_call("analytics.rank", {"metric": "Revenue", "dimension": "Category"})
    assert not m.record_call("analytics.rank", {"dimension": "Region", "metric": "Revenue"})
    assert m.tool_calls == 2


# --- IntentResolver post-checks -----------------------------------------------------------


def _out(**fields: Any) -> IntentOutput:
    base = {"route": "structured", "objective": "metric_value"}
    return IntentOutput.model_validate({**base, **fields})


def test_names_are_canonicalised_and_why_routes_to_explain_change() -> None:
    out = _out(
        metrics=["revenue"],
        dimensions=["category"],
        filters=[{"dimension": "region", "op": "eq", "value": "North"}],
        start="2026-08-01",
        end="2026-08-31",
    )
    resolved = post_check(out, "Why did revenue drop by category?", MODEL)
    assert resolved.intent is not None
    assert resolved.intent.objective == "explain_change"
    assert resolved.intent.metrics == ["Revenue"]
    assert resolved.intent.dimensions == ["Category"]
    assert resolved.intent.filters[0].dimension == "Region"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"route": "unsupported"}, "UNSUPPORTED"),
        ({"route": "simulate"}, "NOT_AVAILABLE"),
        ({"route": "document"}, "NOT_AVAILABLE"),
        ({"metrics": ["Profit Margin"]}, "UNKNOWN_NAME"),
        ({"metrics": ["Revenue"], "dimensions": ["Country"]}, "UNKNOWN_NAME"),
    ],
)
def test_refusals(fields: dict[str, Any], code: str) -> None:
    resolved = post_check(_out(**fields), "q", MODEL)
    assert resolved.refusal is not None
    assert resolved.refusal.code == code
    assert resolved.intent is None


def test_unknown_metric_suggestions_come_from_the_model() -> None:
    resolved = post_check(_out(metrics=["Revenu"]), "q", MODEL)
    assert resolved.refusal is not None
    assert resolved.refusal.suggestions == ["Revenue"]


def test_clarifications_and_document_promotion() -> None:
    assert post_check(_out(route="clarify"), "q", MODEL).clarification
    assert post_check(_out(), "q", MODEL).clarification  # no metric
    bad_range = _out(metrics=["Revenue"], start="2026-09-01", end="2026-08-01")
    assert post_check(bad_range, "q", MODEL).clarification
    mixed = post_check(_out(route="mixed", metrics=["Revenue"]), "q", MODEL)
    assert mixed.intent is not None
    assert mixed.caveats == [NO_DOCUMENTS]


# --- QuestionPlanner ----------------------------------------------------------------------


def _plan(
    objective: Any,
    *,
    time: TimeSpec | None = AUGUST,
    dimensions: list[str] | None = None,
    permissions: frozenset[ToolPermission] = CHAT,
) -> PlanOutcome:
    intent = AnalysisIntent(
        objective=objective,
        metrics=["Revenue"],
        dimensions=dimensions or [],
        time=time,
        source="user_question",
    )
    return QuestionPlanner(REGISTRY, SequentialIds()).plan(intent, MODEL, permissions)


@pytest.mark.parametrize(
    ("objective", "tools", "dimensions"),
    [
        ("metric_value", ["analytics.metric_query"], None),
        ("overview", ["analytics.metric_query"], None),
        ("metric_value", ["analytics.rank"], ["Region"]),
        ("rank", ["analytics.rank"], ["Region"]),
        ("compare", ["analytics.metric_query", "analytics.growth"], None),
        ("trend", ["analytics.trend"], None),
        ("explain_change", ["analytics.explain_change", "analytics.growth"], None),
    ],
)
def test_templates_produce_valid_plans(
    objective: str, tools: list[str], dimensions: list[str] | None
) -> None:
    plan = _plan(objective, dimensions=dimensions)
    assert isinstance(plan, AnalysisPlan), plan
    assert [s.tool for s in plan.steps] == tools


def test_filtered_and_time_dimensions_are_not_groupings() -> None:
    intent = AnalysisIntent(
        objective="metric_value",
        metrics=["Revenue"],
        dimensions=["Region", "Order Date"],
        filters=[{"dimension": "Region", "op": "eq", "value": "North"}],
        time=AUGUST,
        source="user_question",
    )
    plan = QuestionPlanner(REGISTRY, SequentialIds()).plan(intent, MODEL, CHAT)
    assert isinstance(plan, AnalysisPlan)
    assert [s.tool for s in plan.steps] == ["analytics.metric_query"]  # found in the live run


def test_planner_refuses_or_clarifies() -> None:
    forecast = _plan("forecast")
    assert isinstance(forecast, Refusal)
    assert forecast.code == "NOT_AVAILABLE"
    assert isinstance(_plan("trend", time=None), Clarification)
    refused = _plan("trend", permissions=frozenset({ToolPermission.SQL_READ}))
    assert isinstance(refused, Refusal)
    assert refused.code == "INSUFFICIENT_DATA"  # analytics.trend needs PYTHON_ANALYTICS


# --- composer helpers + ClaimExtractor ----------------------------------------------------


def _call(value: Any, unit: str | None, ev: str | None = "ev_a") -> ExecutedCall:
    result = ToolResult(
        tool="analytics.growth",
        tool_version="1.0.0",
        status="ok",
        output={"value": value, "unit": unit},
        evidence_id=ev,
        duration_ms=1,
    )
    return ExecutedCall("S1", "analytics.growth", {"metric": "Revenue"}, result)


def test_facts_skip_non_numeric_and_evidence_less_results() -> None:
    calls = [_call(12.5, "pct"), _call(None, "pct", "ev_b"), _call(True, None, "ev_c")]
    calls.append(_call(3.0, None, None))
    assert [(f.ref, f.evidence_id, f.value) for f in facts_from(calls)] == [("n1", "ev_a", 12.5)]


@pytest.mark.parametrize(
    ("unit", "fmt", "shown"),
    [
        ("pct", "pct:1", "1,250.0%"),  # fractions: 12.5 = 1250 %
        ("pp", "pp:1", "1,250.0 pp"),
        ("count", "count", "12"),
        ("currency:INR", "currency:INR:2", "INR 12.50"),
        (None, "number:2", "12.50"),
        ("kg", "number:2", "12.50"),
    ],
)
def test_formats(unit: str | None, fmt: str, shown: str) -> None:
    assert display_format(unit) == fmt
    assert format_value(12.0 if unit == "count" else 12.5, unit) == shown


def test_growth_fraction_validates_as_percent_text() -> None:
    """TRD 25.1: growth 400 -> 626 is 0.565 (unit pct); '56.5%' in text validates against it."""
    result = growth(400.0, 626.0, "currency:INR")
    assert result.value is not None
    assert result.unit == "pct"
    assert result.value == pytest.approx(0.565)
    ev = factories.evidence("ev_growth", value=result.value, unit=result.unit, arguments={})
    shown = format_value(result.value, result.unit)
    assert shown == "56.5%"

    def check(sentence: str) -> str:
        req = factories.request(factories.text(sentence), evidence_ids=["ev_growth"])
        return factories.run(req, factories.bundle(ev)).status

    assert check(f"Revenue rose {shown} vs July.") == "VALID"
    assert check("Revenue rose 0.6% vs July.") != "VALID"  # the unscaled fraction is wrong


def test_claims_are_numeric_sentences() -> None:
    calls = [_call(540.0, "currency:INR")]
    segments: list[AnswerSegment] = [
        TextSegment(text="Hello. Revenue was "),
        NumberSegment(evidence_id="ev_a", field="result.value", format="currency:INR:2"),
        TextSegment(text=" in August. No number here."),
        CaveatSegment(text="Caveat text."),
    ]
    (claim,) = extract_claims(segments, calls)
    assert claim.text == "Revenue was INR 540.00 in August."
    assert claim.evidence_ids == ["ev_a"]


def test_args_summary_hides_values_and_odd_names() -> None:
    assert args_summary({"metric": "IGNORE ALL", "Evil Key!": 1}) == "?, metric"
