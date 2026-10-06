"""GAPS-5c/5d: WidgetSelector cards for failed/skipped steps and undefined growth (no database)."""

from app.dashboard.domain import KpiCardWidget, UnavailableWidget
from app.dashboard.selector import MetricFilter, Prioritizer, StepOutcome, WidgetSelector
from app.planner.domain import AnalysisStep, CapabilityReport, ReasonCode
from app.planner.runs import StepRecord, StepState
from app.tools.domain import ErrorInfo
from tests.unit.analytics.conftest import semantic_model
from tests.unit.validator.factories import evidence

MODEL = semantic_model()
REPORT = CapabilityReport(
    dataset_version_id="dsv_test",
    semantic_model_version=1,
    rules_version="1.0.0",
    available=["kpi", "trend", "forecast", "anomaly_detection", "rca", "customer_segmentation"],
    unavailable=[],
)


def _step(sid: str, kind: str, tool: str) -> AnalysisStep:
    return AnalysisStep(
        id=sid, type=kind, tool=tool, args={"metric": "Revenue"}, expected_output="x"
    )


def _select(*outcomes: StepOutcome) -> list[object]:
    keep = MetricFilter(MODEL, Prioritizer(), generic_only=False)
    return list(WidgetSelector().select("pl_test", outcomes, REPORT, keep))


def test_failed_and_skipped_steps_become_unavailable_cards_with_the_reason() -> None:
    failed = StepOutcome(
        _step("S1", "aggregate", "analytics.metric_query"),
        StepRecord(
            step_id="S1",
            tool="analytics.metric_query",
            state=StepState.FAILED,
            error=ErrorInfo(code="TOOL_ERROR", message="query failed", retryable=False),
        ),
        None,
        None,
    )
    skipped = StepOutcome(
        _step("S2", "growth_rate", "analytics.growth"),
        StepRecord(step_id="S2", tool="analytics.growth", state=StepState.SKIPPED),
        None,
        None,
    )
    cards = [w for w in _select(failed, skipped) if isinstance(w, UnavailableWidget)]
    by_id = {c.id: c for c in cards}
    assert by_id["u_S1"].reason_code is ReasonCode.ANALYSIS_FAILED
    assert by_id["u_S1"].message == "This analysis could not be completed: query failed."
    assert by_id["u_S1"].title == "Revenue"
    assert by_id["u_S2"].reason_code is ReasonCode.ANALYSIS_FAILED
    assert "did not succeed" in by_id["u_S2"].message


def test_undefined_growth_is_a_kpi_card_that_says_why() -> None:
    record = evidence("ev_growth", value=None, unit="pct", operation="analytics.growth").model_copy(
        update={"calculation": "(x1 - x0) / |x0|; undefined: BASE_IS_ZERO"}
    )
    outcome = StepOutcome(
        _step("S3", "growth_rate", "analytics.growth"),
        StepRecord(
            step_id="S3",
            tool="analytics.growth",
            state=StepState.SUCCEEDED,
            evidence_id="ev_growth",
        ),
        record,
        None,
    )
    (card,) = [w for w in _select(outcome) if isinstance(w, KpiCardWidget)]
    assert card.undefined_reason == "Undefined (base is zero)"
    assert card.value_ref.evidence_id == "ev_growth"
