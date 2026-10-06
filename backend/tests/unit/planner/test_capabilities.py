"""CapabilityEvaluator + rules (TASK-ANALYSIS-002, TASK-INTEGRATION-003): fixture labels, exact
"Insufficient data" messages, config references, determinism."""

from collections.abc import Callable, Sequence
from dataclasses import replace

import pytest

from app.planner.capabilities import CapabilityEvaluator
from app.planner.config import (
    check_references,
    default_analysis_config,
    default_rules,
    load_rules,
)
from app.planner.domain import CapabilityRuleSet, ReasonCode, Unavailability
from app.planner.runs import AnalysisRun, RunState, StepRecord, StepState
from app.planner.service import attempted

from .conftest import Understood

Get = Callable[[str], Understood]
EVALUATOR = CapabilityEvaluator()


def _reasons(unavailable: Sequence[Unavailability]) -> dict[str, set[str]]:
    reasons: dict[str, set[str]] = {}
    for u in unavailable:
        reasons.setdefault(u.analysis, set()).add(u.reason_code.value)
    return reasons


@pytest.mark.parametrize("name", ["retail", "saas", "agriculture", "insufficient"])
def test_reports_match_fixture_labels(understood: Get, name: str) -> None:
    """Every labelled analysis that is also a rule gets the labelled verdict."""
    fixture = understood(name)
    report = EVALUATOR.evaluate(fixture.facts)
    rules = set(default_rules().analyses)
    for analysis in set(fixture.labels["available_analyses"]) & rules:
        assert analysis in report.available, (name, analysis)
    reasons = _reasons(report.unavailable)
    for expected in fixture.labels["unavailable_analyses"]:
        if expected["analysis"] in rules:
            assert expected["reason_code"] in reasons[expected["analysis"]], (name, expected)


def test_insufficient_fixture_says_what_is_missing(understood: Get) -> None:
    report = EVALUATOR.evaluate(understood("insufficient").facts)
    forecast = next(u for u in report.unavailable if u.analysis == "forecast")
    assert forecast.reason_code is ReasonCode.INSUFFICIENT_HISTORY
    assert forecast.message == (
        "Insufficient data: forecasting needs at least 12 monthly periods of history; "
        "the data covers 3."
    )
    assert forecast.would_enable == "a longer history covering more periods"
    assert all(u.message.startswith("Insufficient data: ") for u in report.unavailable)
    segmentation = [u.reason_code for u in report.unavailable if u.analysis.startswith("customer")]
    assert segmentation == [ReasonCode.NO_CUSTOMER_IDENTIFIER]  # the count check does not repeat it
    assert {"kpi", "trend", "anomaly_detection", "rca"} <= set(report.available)


def test_retail_supports_the_full_descriptive_and_ml_set(understood: Get) -> None:
    report = EVALUATOR.evaluate(understood("retail").facts)
    assert report.available == [
        "kpi",
        "trend",
        "forecast",
        "anomaly_detection",
        "customer_segmentation",
        "churn_analysis",  # repeated, dated orders per customer
        "cohort_analysis",
        "rca",
    ]
    assert {u.reason_code for u in report.unavailable} == {ReasonCode.NO_TARGET}
    assert report.rules_version == "1.0.0"
    assert report.semantic_model_version == 1


def test_saas_churn_from_status_and_irregular_starts(understood: Get) -> None:
    report = EVALUATOR.evaluate(understood("saas").facts)
    assert "churn_analysis" in report.available  # status column
    reasons = _reasons(report.unavailable)
    assert reasons["forecast"] == {"IRREGULAR_TIME"}
    assert reasons["cohort_analysis"] == {"NO_RETENTION_HISTORY"}  # one row per customer


def test_yearly_data_counts_years(understood: Get) -> None:
    facts = understood("agriculture").facts
    assert facts.time is not None
    assert (facts.time.planning_grain, facts.time.periods) == ("year", 6)
    assert "forecast" in EVALUATOR.evaluate(facts).available


def test_target_enables_supervised_analyses(understood: Get) -> None:
    facts = understood("retail").facts
    report = EVALUATOR.evaluate(replace(facts, target="channel"))
    assert {"classification", "regression"} <= set(report.available)
    missing = EVALUATOR.evaluate(replace(facts, target="nope"))
    message = next(u.message for u in missing.unavailable if u.analysis == "classification")
    assert "the target column 'nope', which does not exist" in message


def test_evaluation_is_deterministic(understood: Get) -> None:
    facts = understood("saas").facts
    assert EVALUATOR.evaluate(facts) == CapabilityEvaluator().evaluate(facts)


def test_unknown_config_reference_is_rejected_at_load() -> None:
    raw = load_rules().model_dump(mode="json")
    raw["analyses"]["kpi"]["requires"][1]["params"]["min"] = "config.kpi.nope"
    with pytest.raises(ValueError, match="unknown analysis config reference"):
        check_references(CapabilityRuleSet.model_validate(raw), default_analysis_config())


def test_thresholds_are_the_documented_ones() -> None:
    """docs/ML.md lists these; change both together."""
    config = default_analysis_config()
    assert config.version == "analysis-config@1"
    assert config.threshold("forecast", "min_periods") == {"month": 12, "quarter": 8, "year": 5}
    assert config.threshold("forecast", "min_regularity") == 0.8
    assert config.threshold("segmentation", "min_entities") == 50
    assert config.threshold("anomaly", "min_rows") == 30
    assert config.threshold("supervised", "min_rows") == 200
    assert set(default_rules().analyses) == {
        "kpi",
        "trend",
        "forecast",
        "anomaly_detection",
        "customer_segmentation",
        "classification",
        "regression",
        "churn_analysis",
        "cohort_analysis",
        "rca",
    }


def _step(step_id: str, state: StepState, gap: Unavailability | None = None) -> StepRecord:
    return StepRecord(step_id=step_id, tool="ml.segments", state=state, unavailable=gap)


def test_attempted_lists_available_analyses_the_run_refused_once_each() -> None:
    """U-4: post-run outcomes for the capability view (only typed refusals of available ones)."""
    gap = Unavailability(
        analysis="customer_segmentation", reason_code=ReasonCode.LOW_MODEL_QUALITY, message="m"
    )
    other = gap.model_copy(update={"analysis": "classification"})
    steps = [
        _step("s1", StepState.UNAVAILABLE, gap),
        _step("s2", StepState.UNAVAILABLE, gap),
        _step("s3", StepState.UNAVAILABLE, other),
        _step("s4", StepState.FAILED),
        _step("s5", StepState.SUCCEEDED),
    ]
    run = AnalysisRun(
        id="ar_1",
        plan_id="ap_1",
        dataset_version_id="dsv_1",
        semantic_model_version=1,
        state=RunState.PARTIAL,
        steps={s.step_id: s for s in steps},
    )
    available = ["customer_segmentation", "forecast", "trend"]
    assert attempted(run, available) == [gap]
    assert attempted(None, available) == []
