"""TASK-ML-002/003/004/005/007/008: detector, features, splitter, baselines, trainer, evaluator."""

import math

import numpy as np
import polars as pl
import pytest

from app.core.errors import DataError, ModelError
from app.ml.baselines import LinearRegressor, MajorityClass, MeanRegressor, forecast_baseline
from app.ml.config import MLConfig, ResourceConfig
from app.ml.detector import DataCharacteristics, MLTaskDetector
from app.ml.domain import MLTask, TaskSpec
from app.ml.evaluator import (
    classification_metrics,
    clustering_metrics,
    forecast_metrics,
    mase_scale,
    regression_metrics,
    score_supervised,
)
from app.ml.features import FeatureBuilder, lagged
from app.ml.splitter import kfold, stratified, temporal
from app.ml.trainer import Trainer
from app.planner.domain import CapabilityReport, ReasonCode, Unavailability

FORECAST = TaskSpec(
    task=MLTask.FORECAST,
    dataset_version_id="dsv_1",
    metric="sum(x)",
    time_column="d",
    horizon=3,
    seed=1,
)


def _series(**kw: object) -> DataCharacteristics:
    base: dict[str, object] = {
        "rows": 100,
        "has_time": True,
        "grain": "month",
        "periods": 24,
        "regularity": 1.0,
        "horizon": 3,
    }
    return DataCharacteristics(**(base | kw))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("chars", "code"),
    [
        (_series(has_time=False), ReasonCode.NO_TIME_DIMENSION),
        (_series(grain="hour"), ReasonCode.IRREGULAR_TIME),
        (_series(regularity=0.5), ReasonCode.IRREGULAR_TIME),
        (_series(periods=3), ReasonCode.INSUFFICIENT_HISTORY),
        (_series(missing_ratio=0.4), ReasonCode.TOO_MANY_MISSING_PERIODS),
        (_series(horizon=20), ReasonCode.INSUFFICIENT_HISTORY),
    ],
)
def test_forecast_refusals_carry_specific_reasons(
    chars: DataCharacteristics, code: ReasonCode
) -> None:
    out = MLTaskDetector().detect(None, FORECAST, chars)
    assert isinstance(out, Unavailability)
    assert out.reason_code is code
    assert out.analysis == "forecast"


def test_forecast_allowed_and_capability_report_wins() -> None:
    assert not isinstance(MLTaskDetector().detect(None, FORECAST, _series()), Unavailability)
    caps = CapabilityReport(
        dataset_version_id="dsv_1",
        semantic_model_version=1,
        rules_version="0.1.0",
        available=[],
        unavailable=[
            Unavailability(
                analysis="forecast", reason_code=ReasonCode.INSUFFICIENT_HISTORY, message="3 mo"
            )
        ],
    )
    out = MLTaskDetector().detect(caps, FORECAST, _series())
    assert isinstance(out, Unavailability)
    assert out.message == "3 mo"


def test_other_task_refusals() -> None:
    det = MLTaskDetector()
    anomaly = TaskSpec(task=MLTask.ANOMALY, dataset_version_id="dsv_1", seed=1)
    seg = TaskSpec(task=MLTask.SEGMENTATION, dataset_version_id="dsv_1", seed=1)
    cls = TaskSpec(
        task=MLTask.CLASSIFICATION, dataset_version_id="dsv_1", target="y", features=["a"], seed=1
    )
    cases = [
        (anomaly, DataCharacteristics(rows=3), ReasonCode.INSUFFICIENT_ROWS),
        (
            anomaly,
            DataCharacteristics(rows=50, multivariate=True, numeric_features=1),
            ReasonCode.NO_NUMERIC_FEATURES,
        ),
        (
            seg,
            DataCharacteristics(rows=50, entities=5, numeric_features=2),
            ReasonCode.INSUFFICIENT_ENTITIES,
        ),
        (seg, DataCharacteristics(rows=50, entities=50), ReasonCode.NO_NUMERIC_FEATURES),
        (cls, DataCharacteristics(rows=500), ReasonCode.NO_TARGET),
    ]
    for spec, chars, code in cases:
        out = det.detect(None, spec, chars)
        assert isinstance(out, Unavailability)
        assert out.reason_code is code


def test_temporal_split_never_leaks_future() -> None:
    splits = temporal(n=24, folds=3, horizon=3, min_train=6)
    assert len(splits) == 3
    for s in splits:
        assert s.train.max() < s.test.min()  # every training index precedes every test index
        assert list(s.train) == list(range(len(s.train)))  # expanding window from the start
    assert splits[-1].test[-1] == 23
    assert temporal(n=8, folds=3, horizon=3, min_train=6) == []


def test_kfold_and_stratified_are_seeded_partitions() -> None:
    a, b = kfold(20, 4, seed=7), kfold(20, 4, seed=7)
    assert all((x.test == y.test).all() for x, y in zip(a, b, strict=True))
    assert sorted(np.concatenate([s.test for s in a]).tolist()) == list(range(20))
    y = np.array([0] * 15 + [1] * 5)
    for s in stratified(y, 5, seed=3):
        assert y[s.test].sum() == 1  # each fold keeps the 3:1 class ratio


def test_feature_builder_fits_on_train_only() -> None:
    train = pl.DataFrame({"x": [1.0, 2.0, 3.0, None], "c": ["a", "b", "a", "a"]})
    test = pl.DataFrame({"x": [1000.0, -5.0], "c": ["z", "b"]})
    fitted = FeatureBuilder().fit(train, numeric=["x"], categorical=["c"])
    assert fitted.medians == (2.0,)  # the null is imputed with the train median
    assert fitted.means[0] == pytest.approx(2.0)
    before = fitted.transform(train)
    out = fitted.transform(test)
    assert fitted == FeatureBuilder().fit(train, numeric=["x"], categorical=["c"])
    assert (fitted.transform(train) == before).all()  # transforming test changed nothing
    assert out[0, 1:].tolist() == [0.0, 0.0]  # unseen category "z" -> all zeros
    assert fitted.names == ["x", "c=a", "c=b"]
    with pytest.raises(DataError):
        FeatureBuilder().fit(train, numeric=["missing"])


def test_lags_use_only_earlier_rows() -> None:
    frame = pl.DataFrame({"t": [3, 1, 2], "y": [30.0, 10.0, 20.0]})
    out = lagged(frame, "y", [1], order_by="t")
    assert out["y_lag1"].to_list() == [None, 10.0, 20.0]
    with pytest.raises(DataError, match="leak"):
        lagged(frame, "y", [0], order_by="t")


def test_supervised_baselines_hand_computed() -> None:
    x = np.array([[0.0], [1.0], [2.0], [3.0]])
    assert MajorityClass().fit(x, np.array(["b", "a", "a", "b"])).predict(x[:1])[0] == "a"
    assert MeanRegressor().fit(x, np.array([1.0, 2.0, 3.0, 6.0])).predict(x[:1])[0] == 3.0
    line = LinearRegressor().fit(x, 2 * x[:, 0] + 1).predict(np.array([[10.0]]))
    assert line[0] == pytest.approx(21.0)
    assert forecast_baseline(12) == "seasonal_naive"
    assert forecast_baseline(None) == "naive"


def test_score_supervised_reports_model_and_baseline_on_same_splits() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(60, 1))
    y = 3 * x[:, 0] + rng.normal(scale=0.1, size=60)
    m = score_supervised(
        "regression", LinearRegressor, MeanRegressor, "mean", x, y, kfold(60, 5, seed=0)
    )
    assert m.primary == "mae"
    assert m.baseline is not None
    assert m.values["mae"] < m.baseline.values["mae"]


def test_forecast_metrics_hand_computed() -> None:
    actual, pred = np.array([10.0, 0.0, 5.0]), np.array([8.0, 0.0, 7.0])
    m = forecast_metrics(actual, pred, scale=2.0, lower=pred - 1, upper=pred + 1)
    assert m["mae"] == pytest.approx(4 / 3)
    assert m["rmse"] == pytest.approx(math.sqrt(8 / 3))
    assert m["smape"] == pytest.approx((4 / 18 + 0 + 4 / 12) / 3)  # 0/0 term counts 0
    assert m["mase"] == pytest.approx(2 / 3)
    assert m["coverage"] == pytest.approx(1 / 3)
    assert "mase" not in forecast_metrics(actual, pred, scale=0.0)  # never infinite
    assert mase_scale(np.array([1.0, 3.0, 2.0, 6.0]), None) == pytest.approx(7 / 3)
    assert mase_scale(np.array([1.0, 3.0, 2.0, 6.0]), 2) == pytest.approx(2.0)


def test_other_metrics_hand_computed() -> None:
    c = classification_metrics(np.array([1, 1, 0, 0]), np.array([1, 0, 0, 0]))
    assert c["accuracy"] == 0.75
    r = regression_metrics(np.array([1.0, 2.0]), np.array([1.0, 4.0]))
    assert r == {"mae": 1.0, "rmse": math.sqrt(2.0), "r2": pytest.approx(-7.0)}
    x = np.array([[0.0], [0.1], [10.0], [10.1]])
    k = clustering_metrics(x, np.array([0, 0, 1, 1]), seed=0, sample=100)
    assert k["silhouette"] > 0.9


def test_trainer_downsamples_with_caveat_or_refuses() -> None:
    trainer = Trainer(MLConfig(resources=ResourceConfig(max_cost_units=10_000)))
    x = np.arange(200.0).reshape(100, 2)
    fitted = trainer.fit("isolation_forest", lambda m: len(m), x, seed=1, can_downsample=True)
    assert fitted.model == 25  # budget 10k / (2 features x 200 units per row)
    assert fitted.rows is not None
    assert "sample of 25 of 100" in fitted.caveats[0]
    again = trainer.fit("isolation_forest", lambda m: len(m), x, seed=1, can_downsample=True)
    assert again.rows is not None
    assert (again.rows == fitted.rows).all()  # seeded
    with pytest.raises(ModelError, match="cannot be down-sampled"):
        trainer.fit("arima", lambda m: m, np.arange(100.0), seed=1, can_downsample=False)

    def boom(_: object) -> None:
        raise ValueError("bad")

    with pytest.raises(ModelError, match="could not be fitted"):
        Trainer().fit("ets", boom, np.arange(5.0), seed=1, can_downsample=False)
