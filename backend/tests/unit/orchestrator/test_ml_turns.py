"""GAPS-2: forecast and anomaly questions in chat (FakeLLM, real ML tools, real validator).

The ML tools run on the retail / insufficient evals fixtures understood through the production
path (tests/unit/planner/conftest.py); only their dataset read and model-run insert are swapped
for in-memory versions. Numbers in the answer come from code (app.orchestrator.ml_answers), cite
the evidence's stored result rows and must validate.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest
from pydantic import BaseModel, JsonValue

from app.core.ids import SequentialIds
from app.core.scope import WorkspaceScope
from app.infra.db import UnitOfWork
from app.infra.storage import LocalStorage, Storage
from app.ml.anomaly_tool import AnomalyOutput, AnomalyTool
from app.ml.domain import ANOMALY_CAVEAT, Metrics, MLTask, ModelInfo, ModelRun, Selection
from app.ml.forecast_tool import ForecastOutput, ForecastTool
from app.orchestrator.domain import FinalEvent, NumberView, SegmentEvent, TurnState
from app.orchestrator.ml_answers import ESTIMATE_CAVEAT
from app.planner.facts import DataFacts
from app.tools.domain import Tool
from app.validator.domain import CaveatSegment, TextSegment, ValidationStatus
from tests.unit.orchestrator.fakes import DONE, Harness, compose, harness, intent, num, text
from tests.unit.planner.conftest import understand

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def in_memory[T: (ForecastTool, AnomalyTool)](cls: type[T], frame: pl.DataFrame) -> T:
    class Memory(cls):  # type: ignore[valid-type,misc]
        def _sync(self, args: BaseModel, scope: WorkspaceScope, version_id: str) -> Any:
            return self.run(args, frame, scope, version_id)

        def persist(
            self,
            scope: WorkspaceScope,
            version_id: str,
            task: MLTask,
            model: ModelInfo,
            args: BaseModel,
            metrics: Metrics,
            seed: int,
            selection: Selection | None = None,
            extra: dict[str, JsonValue] | None = None,
        ) -> ModelRun:
            return ModelRun(
                id=self._ids.new("mr"),
                workspace_id=scope.workspace_id,
                task=task,
                params={},
                selection=selection,
                metrics=metrics,
                seed=seed,
                created_at=NOW,
            )

    tool: T = Memory(cast(UnitOfWork, None), cast(Storage, None), SequentialIds())
    return tool


Dataset = tuple[DataFacts, pl.DataFrame]


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Dataset]:
    cache: dict[str, Dataset] = {}

    def get(name: str) -> Dataset:
        if name not in cache:
            tmp: Path = tmp_path_factory.mktemp(name)
            facts = understand(name, tmp).facts
            with LocalStorage(tmp / "store").open_read("v.parquet") as f:
                cache[name] = (facts, pl.read_parquet(f))
        return cache[name]

    return get


def _harness(dataset: Dataset, *script: Any, question: str) -> Harness:
    facts, frame = dataset
    tools: list[Tool] = [in_memory(ForecastTool, frame), in_memory(AnomalyTool, frame)]
    return harness(
        list(script),
        question,
        table=pl.DataFrame().to_arrow(),
        extra_tools=tools,
        model=facts.model,
        facts=facts,
    )


def _shown(h: Harness) -> list[Any]:
    return [e.segment for e in h.run() if isinstance(e, SegmentEvent)]


def test_forecast_answer_shows_estimates_with_intervals_and_validates(
    dataset: Callable[[str], Dataset],
) -> None:
    h = _harness(
        dataset("retail"),
        intent(objective="forecast", metrics=["Revenue"]),
        DONE,
        compose(text("Here is the revenue outlook.")),
        question="Forecast revenue for the next months",
    )
    shown = _shown(h)
    assert [name for name, _, _ in h.executor.runs] == ["ml.forecast"]
    args = h.executor.runs[0][1]
    assert (args["metric"], args["multiply_by"]) == ("quantity", ["unit_price"])  # pack mapping
    final = h.validation.results[-1]
    assert final.status is ValidationStatus.VALID, final.issues
    numbers = [s for s in shown if isinstance(s, NumberView)]
    texts = "".join(s.text for s in shown if isinstance(s, TextSegment))
    points = ForecastOutput.model_validate(h.executor.runs[0][2].output).forecast.points
    assert len(numbers) == 3 * len(points)  # value, lower, upper per period
    assert [n.value for n in numbers[:3]] == [points[0].value, points[0].lower, points[0].upper]
    assert texts.count("estimate") == len(points)
    assert "prediction interval" in texts
    assert CaveatSegment(text=ESTIMATE_CAVEAT) in shown
    assert h.recorder.records[0].state is TurnState.COMPLETED


def _anomaly_turn(data: Dataset) -> tuple[Harness, list[Any]]:
    h = _harness(
        data,
        intent(objective="anomalies", metrics=["Quantity"]),
        DONE,
        compose(text("I checked the monthly quantity for unusual months.")),
        question="Were there any unusual months for quantity?",
    )
    shown = _shown(h)
    assert [name for name, _, _ in h.executor.runs] == ["ml.anomalies"]
    assert h.validation.results[-1].status is ValidationStatus.VALID, h.validation.results[-1]
    assert CaveatSegment(text=ANOMALY_CAVEAT) in shown
    texts = "".join(s.text for s in shown if isinstance(s, TextSegment))
    assert "problem" not in texts.replace(ANOMALY_CAVEAT, "")
    return h, shown


def test_anomaly_answer_without_flags_still_carries_the_caveat(
    dataset: Callable[[str], Dataset],
) -> None:
    h, shown = _anomaly_turn(dataset("retail"))
    output = h.executor.runs[0][2].output
    assert output is not None
    assert output["flagged"] == 0
    assert TextSegment(text="No statistical anomalies were detected (robust z).") in shown


def test_flagged_anomalies_cite_observed_values_and_validate(
    dataset: Callable[[str], Dataset],
) -> None:
    facts, frame = dataset("retail")
    months = frame["order_date"].dt.strftime("%Y-%m")
    spike_month = sorted(months.unique().to_list())[len(months.unique()) // 2]
    spiked = frame.with_columns(
        pl.when(months == spike_month)
        .then(pl.col("quantity") * 40)
        .otherwise(pl.col("quantity"))
        .alias("quantity")
    )
    h, shown = _anomaly_turn((facts, spiked))
    output = AnomalyOutput.model_validate(h.executor.runs[0][2].output)
    assert output.flagged >= 1
    numbers = [s for s in shown if isinstance(s, NumberView)]
    assert numbers[0].field == "rows.0.observed"
    assert numbers[0].value == output.result.anomalies[0].observed
    assert any(spike_month in s.text for s in shown if isinstance(s, TextSegment))


def test_forecast_without_enough_history_is_refused_with_the_reason(
    dataset: Callable[[str], Dataset],
) -> None:
    facts, _ = dataset("insufficient")
    metric = facts.model.metrics[0].name if facts.model.metrics else facts.model.measures[0].name
    h = _harness(
        dataset("insufficient"),
        intent(objective="forecast", metrics=[metric]),
        question="Forecast next quarter",
    )
    shown = _shown(h)
    assert h.executor.runs == []
    assert h.recorder.records[0].state is TurnState.REFUSED
    assert shown == [
        TextSegment(
            text="Insufficient data: forecasting needs at least 12 monthly periods of history; "
            "the data covers 3."
        )
    ]


def test_forecast_without_facts_or_with_filters_is_not_available(
    dataset: Callable[[str], Dataset],
) -> None:
    no_facts = harness([intent(objective="forecast", metrics=["Revenue"])], "Forecast revenue")
    no_facts.run()
    assert no_facts.recorder.records[0].state is TurnState.REFUSED
    assert no_facts.recorder.records[0].trace["refusal"] == "NOT_AVAILABLE"
    north = [{"dimension": "Region", "op": "eq", "value": "North"}]
    filtered = _harness(
        dataset("retail"),
        intent(objective="forecast", metrics=["Revenue"], filters=north),
        question="Forecast revenue in North",
    )
    filtered.run()
    assert filtered.executor.runs == []  # never answered for the whole dataset instead
    assert filtered.recorder.records[0].trace["refusal"] == "NOT_AVAILABLE"


def test_forecast_said_unavailable_but_listed_is_recomposed(
    dataset: Callable[[str], Dataset],
) -> None:
    """B-3 (live demo): "... is unavailable." followed by the forecast must not pass as VALID."""
    h = _harness(
        dataset("retail"),
        intent(objective="forecast", metrics=["Revenue"]),
        DONE,
        compose(text("The forecast for revenue over the next three months is unavailable.")),
        compose(text("Here is the revenue outlook.")),
        question="Forecast revenue for the next 3 months.",
    )
    shown = _shown(h)
    first, final = h.validation.results[0], h.validation.results[-1]
    assert first.status is ValidationStatus.INVALID
    assert "CONTRADICTION_AVAILABILITY" in {i.code for i in first.issues}
    assert final.status is ValidationStatus.VALID, final.issues
    texts = [s.text for s in shown if isinstance(s, TextSegment)]
    assert texts[0] == "Here is the revenue outlook."
    assert texts[1].startswith(("\n", " "))  # no "outlook.Forecast by month" run-on


def _spiked(data: Dataset) -> Dataset:
    facts, frame = data
    months = frame["order_date"].dt.strftime("%Y-%m")
    spike_month = sorted(months.unique().to_list())[len(months.unique()) // 2]
    quantity = pl.when(months == spike_month).then(pl.col("quantity") * 40)
    return facts, frame.with_columns(quantity.otherwise(pl.col("quantity")).alias("quantity"))


def test_composer_can_cite_a_flagged_anomaly(dataset: Callable[[str], Dataset]) -> None:
    """B-2 (live demo): the model cites the anomaly with a number segment; it must bind to the
    flagged point (rows.0.observed), not fail with "segments.1.ref: must be one of none"."""
    h = _harness(
        _spiked(dataset("retail")),
        intent(objective="anomalies", metrics=["Quantity"]),
        DONE,
        compose(text("The most unusual month had a quantity of "), num("n1"), text(".")),
        question="Were there any unusual months for quantity?",
    )
    shown = _shown(h)
    record = h.recorder.records[0]
    assert record.state is TurnState.COMPLETED, record.trace
    assert h.validation.results[-1].status is ValidationStatus.VALID
    output = AnomalyOutput.model_validate(h.executor.runs[0][2].output)
    first = next(s for s in shown if isinstance(s, NumberView))
    assert (first.field, first.value) == ("rows.0.observed", output.result.anomalies[0].observed)


def test_bad_anomaly_compose_falls_back_to_the_anomaly_block(
    dataset: Callable[[str], Dataset],
) -> None:
    """B-2 fallback: an unusable composed answer keeps the code-built anomaly block and caveat."""
    bad = compose(text("Unusual: "), num("n9"))
    h = _harness(
        dataset("retail"),
        intent(objective="anomalies", metrics=["Quantity"]),
        DONE,
        bad,
        bad,
        question="Were there any unusual months for quantity?",
    )
    shown = _shown(h)
    assert h.recorder.records[0].state is TurnState.RECOVERED
    assert h.validation.results[-1].status is ValidationStatus.VALID
    assert TextSegment(text="No statistical anomalies were detected (robust z).") in shown
    assert CaveatSegment(text=ANOMALY_CAVEAT) in shown


@pytest.mark.parametrize(
    ("objective", "metric", "wording"),
    [
        ("forecast", "Revenue", "Revenue will definitely keep growing."),  # GUARANTEE_LANGUAGE
        ("anomalies", "Quantity", "The unusual months were caused by promotions."),  # CAUSAL
    ],
)
def test_ml_wording_failure_twice_is_answered_from_the_ml_results(
    dataset: Callable[[str], Dataset], objective: str, metric: str, wording: str
) -> None:
    """RCA-FIX item 3: the same wording-only fallback as explain_change (fallback.py)."""
    bad = compose(text(wording))
    h = _harness(
        _spiked(dataset("retail")),
        intent(objective=objective, metrics=[metric]),
        DONE,
        bad,
        bad,
        question=f"Tell me about the {objective} of {metric}",
    )
    events = h.run()
    final = events[-1]
    assert isinstance(final, FinalEvent)
    statuses = [r.status for r in h.validation.results]
    assert statuses == [
        ValidationStatus.INVALID,
        ValidationStatus.REQUIRES_REVIEW,
        ValidationStatus.VALID,
    ], h.validation.results[-1].issues
    assert (final.state, final.answer_source) == (TurnState.COMPLETED, "template")
    shown = [e.segment for e in events if isinstance(e, SegmentEvent)]
    texts = "".join(s.text for s in shown if isinstance(s, TextSegment))
    assert wording not in texts
    assert any(isinstance(s, NumberView) for s in shown)
