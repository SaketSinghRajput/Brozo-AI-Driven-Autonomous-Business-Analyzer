"""ML-014/019/022 tools on the evals fixtures (real ingestion Parquet, no database).

Seeded ground truth (scripts/generate_fixtures.py, evals/fixtures/retail/labels.json):
QUANTITY_OUTLIER O02584 (qty 250), PRICE_OUTLIER O04168 (8x price), VOLUME_SPIKE_DAY
2025-11-20 (35 extra East orders), North-region revenue drop in 2026-08 (Electronics x0.35).
The insufficient fixture holds 3 months: a monthly forecast must be refused.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest

from app.infra.storage import Storage
from app.ml.anomaly_tool import AnomalyArgs, AnomalyOutput, AnomalyTool
from app.ml.domain import ANOMALY_CAVEAT
from app.ml.forecast_tool import ForecastArgs, ForecastOutput, ForecastTool
from app.ml.segments_tool import SegmentsArgs, SegmentsOutput, SegmentsTool
from app.ml.tooling import MLTool
from app.planner.capabilities import CapabilityEvaluator
from app.planner.default_planner import DefaultPlanner
from app.planner.domain import ReasonCode, Unavailability
from app.tools.registry import ToolRegistry
from tests.unit.data.conftest import FIXTURES
from tests.unit.planner.conftest import understand

from .conftest import SCOPE, VERSION, tool_deps

Frames = Callable[[str], pl.DataFrame]
LABELS = json.loads((FIXTURES / "retail" / "labels.json").read_text(encoding="utf-8"))
TRUTH_ROWS = {a["order_id"] for a in LABELS["anomaly_ground_truth"]}
NO_STORAGE = cast(Storage, object())  # run() receives the frame; storage is never read


def _tool[T: MLTool](cls: type[T]) -> tuple[T, list[dict[str, Any]]]:
    uow, fake, ids, clock = tool_deps()
    return cls(uow, NO_STORAGE, ids, clock), fake.session.statements


def _anomalies(frames: Frames, **args: Any) -> AnomalyOutput:
    tool, _ = _tool(AnomalyTool)
    out = tool.run(AnomalyArgs.model_validate(args), frames("retail/orders.csv"), SCOPE, VERSION)
    assert isinstance(out, AnomalyOutput)
    assert ANOMALY_CAVEAT in out.result.explanation.caveats
    return out


def test_forecast_monthly_revenue_estimate_vs_baseline(ingested: Frames) -> None:
    tool, writes = _tool(ForecastTool)
    args = ForecastArgs(
        time_column="order_date", metric="quantity", multiply_by=["unit_price"], horizon=3
    )
    out = tool.run(args, ingested("retail/orders.csv"), SCOPE, VERSION)
    assert isinstance(out, ForecastOutput)
    f = out.forecast
    assert f.label == "estimate"
    assert f.grain == "month"
    assert len(f.points) == 3
    assert f.interval_level == 0.9
    assert all(p.lower is not None and p.upper is not None for p in f.points)
    metrics = out.model_result.metrics
    assert metrics.baseline is not None
    assert metrics.baseline.name == "naive"  # no significant seasonality in 24 months
    if out.selection.fell_back_to_baseline:
        assert out.selection.best == "naive"
    else:  # chosen only when it beat the baseline by the margin
        assert metrics.values["mae"] <= 0.95 * metrics.baseline.values["mae"]
    assert writes[0]["id"] == out.model_run_id  # one model run persisted
    assert writes[0]["dsv"] == VERSION
    assert out.model_run_id in out.calculation
    print(  # fixture metric recorded in the ML report
        "retail forecast",
        out.selection.best,
        metrics.values.get("mase"),
        metrics.baseline.values.get("mase"),
    )
    again = tool.run(args, ingested("retail/orders.csv"), SCOPE, VERSION)
    assert isinstance(again, ForecastOutput)
    # Reproducible: same model and selection; values agree to floating-point noise (statsmodels'
    # L-BFGS MLE differed at ~1e-9 relative between identical in-process runs; see ML-models.md).
    assert again.selection == out.selection or again.selection.best == out.selection.best
    for a, b in zip(again.forecast.points, f.points, strict=True):
        assert a.period == b.period
        assert a.value == pytest.approx(b.value, rel=1e-6)
        assert a.upper == pytest.approx(b.upper, rel=1e-6)


def test_forecast_refused_on_three_months_of_history(ingested: Frames) -> None:
    tool, writes = _tool(ForecastTool)
    args = ForecastArgs(time_column="date", metric="quantity", horizon=3, grain="month")
    out = tool.run(args, ingested("insufficient/sales.csv"), SCOPE, VERSION)
    assert isinstance(out, Unavailability)
    assert out.reason_code is ReasonCode.INSUFFICIENT_HISTORY
    assert "3 month periods" in out.message
    assert writes == []  # refused: no model run


def test_point_anomalies_recover_seeded_quantity_and_price_outliers(ingested: Frames) -> None:
    qty = _anomalies(ingested, metric="quantity", id_column="order_id")
    assert [a.subject["order_id"] for a in qty.result.anomalies] == ["O02584"]
    assert qty.result.explanation.method == "robust_z"
    price = _anomalies(ingested, metric="unit_price", dimension="product_id", id_column="order_id")
    found = {str(a.subject["order_id"]) for a in price.result.anomalies}
    assert "O04168" in found
    precision = len(found & TRUTH_ROWS) / len(found)
    assert precision == 1.0  # the other flags are spike-day rows (grocery price reused)
    print("price contextual precision", precision, "flagged", price.flagged)


def test_daily_series_finds_the_volume_spike_day(ingested: Frames) -> None:
    out = _anomalies(ingested, time_column="order_date", agg="count")
    assert [a.subject["period"] for a in out.result.anomalies] == ["2025-11-20"]
    assert out.result.anomalies[0].direction == "high"


def test_contextual_region_revenue_finds_north_drop_and_south_spike(ingested: Frames) -> None:
    out = _anomalies(
        ingested,
        time_column="order_date",
        grain="month",
        metric="quantity",
        multiply_by=["unit_price"],
        dimension="region",
    )
    hits = {(a.subject["period"], a.subject["region"], a.direction) for a in out.result.anomalies}
    assert hits == {("2026-08-01", "North", "low"), ("2025-03-01", "South", "high")}
    assert out.result.explanation.method == "contextual"


def test_isolation_forest_is_reproducible_and_records_contamination(ingested: Frames) -> None:
    args = {"features": ["quantity", "unit_price", "discount_pct"], "id_column": "order_id"}
    a, b = _anomalies(ingested, **args), _anomalies(ingested, **args)
    assert a.result == b.result
    assert a.model_result.model.params["contamination"] == 0.01
    assert any("contamination" in c for c in a.result.explanation.caveats)
    print("isolation forest flagged", a.flagged, a.result.explanation.contributing_features)


def test_segments_on_retail_customers_are_gated(ingested: Frames) -> None:
    tool, _ = _tool(SegmentsTool)
    args = SegmentsArgs(
        entity_column="customer_id", measures=["quantity", "unit_price"], time_column="order_date"
    )
    out = tool.run(args, ingested("retail/orders.csv"), SCOPE, VERSION)
    if isinstance(out, Unavailability):  # synthetic customers buy at random: no real segments
        assert out.reason_code is ReasonCode.LOW_MODEL_QUALITY
        print("retail segments refused:", out.message)
    else:
        assert isinstance(out, SegmentsOutput)
        assert out.model_result.metrics.values["silhouette"] >= 0.25
        print("retail segments", out.selection.best, out.model_result.metrics.values)


def test_agriculture_plan_ml_steps_accept_an_integer_year(ingested: Frames, tmp_path: Path) -> None:
    """B-1 (live demo): the capability rules plan forecast/anomalies on the integer ``year``
    time dimension; the tools must run (or refuse with a reason), never fail with
    "column 'year' is not a date" (ANALYSIS_FAILED)."""
    facts = understand("agriculture", tmp_path).facts
    report = CapabilityEvaluator().evaluate(facts)
    registry = ToolRegistry(
        [cls(cast(Any, None), cast(Any, None)) for cls in (ForecastTool, AnomalyTool)]
    )
    plan = DefaultPlanner(registry).plan(facts, report).plan
    steps = [s for s in plan.steps if s.tool in ("ml.forecast", "ml.anomalies")]
    assert {s.tool for s in steps} == {"ml.forecast", "ml.anomalies"}
    frame = ingested("agriculture/production.csv")
    for step in steps:
        out: object
        if step.tool == "ml.forecast":
            forecast, _ = _tool(ForecastTool)
            out = forecast.run(ForecastArgs.model_validate(step.args), frame, SCOPE, VERSION)
        else:
            anomaly, _ = _tool(AnomalyTool)
            out = anomaly.run(AnomalyArgs.model_validate(step.args), frame, SCOPE, VERSION)
        if isinstance(out, ForecastOutput):
            assert out.forecast.grain == "year"
            assert str(out.forecast.points[0].period).startswith("2025")
        else:
            assert isinstance(out, AnomalyOutput | Unavailability), step.tool
        print("agriculture", step.tool, type(out).__name__, getattr(out, "message", ""))


@pytest.mark.parametrize(
    "bad",
    [
        {"method": "contextual", "metric": "quantity"},
        {"method": "isolation_forest", "features": ["quantity"]},
        {"agg": "count"},
    ],
)
def test_anomaly_args_reject_incoherent_requests(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"needs|required|counting"):
        AnomalyArgs.model_validate(bad)
