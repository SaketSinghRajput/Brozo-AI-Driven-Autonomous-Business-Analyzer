"""TASK-ML-010..013: series detection, preprocessing, forecasters, backtest and selection."""

import datetime as dt
from statistics import NormalDist

import numpy as np
import polars as pl
import pytest

from app.core.errors import DataError
from app.ml.forecasters import Arima, Ets, Forecaster, Naive, SeasonalNaive, StlEts
from app.ml.predictor import forecast_points
from app.ml.selector import forecast_splits, select_forecaster
from app.ml.timeseries import TimeSeries, build_series, detect_season, next_periods, prepare
from app.ml.trainer import Trainer


def _seasonal(n: int = 32, seed: int = 0, m: int = 4) -> np.ndarray:
    t = np.arange(n)
    rng = np.random.default_rng(seed)
    return np.asarray(100 + 0.5 * t + 20 * np.sin(2 * np.pi * t / m) + rng.normal(0, 1, n))


def test_build_series_marks_empty_periods_missing_never_zero() -> None:
    frame = pl.DataFrame(
        {
            "d": [dt.date(2026, 1, 5), dt.date(2026, 1, 20), dt.date(2026, 3, 2)],
            "q": [1, 2, 4],
            "p": [10.0, 10.0, 0.5],
        }
    )
    ts = build_series(frame, "d", "q", ["p"], "sum", "month")
    assert ts.periods == [dt.date(2026, 1, 1), dt.date(2026, 2, 1), dt.date(2026, 3, 1)]
    assert ts.values[0] == 30.0
    assert np.isnan(ts.values[1])  # February had no rows: missing, not 0
    assert ts.values[2] == 2.0
    assert ts.missing_ratio == pytest.approx(1 / 3)
    assert build_series(frame, "d", None, [], "count", None).grain == "month"  # daily -> month
    with pytest.raises(DataError):
        build_series(frame, "q", "q", [], "sum", None)  # not a date column


def test_prepare_fill_policies_and_outlier_flags() -> None:
    values = np.array([10.0, np.nan, 12.0, 11.0, 90.0, 10.0, 11.0, 12.0])
    ts = TimeSeries([dt.date(2026, 1, 1)] * len(values), values, "month", "month", 1.0, 8)
    out = prepare(ts, "interpolate", 3.5)
    assert out.y[1] == pytest.approx(11.0)
    assert out.filled == 1
    assert 4 in out.outliers  # flagged ...
    assert out.y[4] == 90.0  # ... but kept
    assert prepare(ts, "zero", 3.5).y[1] == 0.0
    with pytest.raises(DataError, match="refuse"):
        prepare(ts, "refuse", 3.5)


def test_season_detected_only_when_strong_and_long_enough() -> None:
    assert detect_season(_seasonal(60, m=12), "month")[0] == 12
    rng = np.random.default_rng(1)
    assert detect_season(100 + rng.normal(0, 1, 60), "month")[0] is None
    assert detect_season(_seasonal(24, m=12), "month") == (None, None)  # < 3 cycles: not measured


def test_naive_intervals_hand_computed() -> None:
    z = NormalDist().inv_cdf(0.95)
    p = Naive().fit(np.array([1.0, 3.0, 2.0, 4.0])).predict(2, 0.9)  # sigma = sqrt(3)
    assert p.mean.tolist() == [4.0, 4.0]
    assert p.upper[1] - p.mean[1] == pytest.approx(z * np.sqrt(3) * np.sqrt(2))
    sp = SeasonalNaive(2).fit(np.array([1.0, 5.0, 2.0, 6.0, 3.0, 7.0])).predict(3, 0.9)
    assert sp.mean.tolist() == [3.0, 7.0, 3.0]
    assert sp.upper[2] - sp.mean[2] == pytest.approx(z * 1.0 * np.sqrt(2))  # residuals all 1


@pytest.mark.parametrize("make", [lambda: Ets(4), lambda: Arima(4), lambda: StlEts(4)])
def test_statsmodels_candidates_are_deterministic_with_intervals(make: type[Forecaster]) -> None:
    y = _seasonal(24)
    a, b = make().fit(y).predict(6, 0.9), make().fit(y).predict(6, 0.9)
    assert np.array_equal(a.mean, b.mean)
    assert (a.lower <= a.mean).all()
    assert (a.mean <= a.upper).all()


def test_backtest_selection_beats_baseline_on_seasonal_trend() -> None:
    y = _seasonal(32)
    splits = forecast_splits(len(y), 4)
    first = select_forecaster(y, 4, splits, 42, Trainer(), {})
    again = select_forecaster(y, 4, splits, 42, Trainer(), {})
    assert first.selection == again.selection  # same input -> same metrics
    assert first.baseline.name == "seasonal_naive"
    assert not first.selection.fell_back_to_baseline
    assert first.best.values["mae"] < first.baseline.values["mae"]
    assert set(first.best.values) >= {"mae", "rmse", "smape", "mase", "coverage"}


def test_selection_keeps_baseline_unless_beaten_by_the_margin() -> None:
    rng = np.random.default_rng(3)
    walk = np.cumsum(rng.normal(0, 5, 40)) + 100  # random walk: naive is optimal
    sel = select_forecaster(walk, None, forecast_splits(40, 4), 42, Trainer(), {})
    if sel.selection.fell_back_to_baseline:
        assert sel.best.name == "naive"
    else:
        assert sel.best.values["mae"] <= 0.95 * sel.baseline.values["mae"]
    assert "stl_ets" in {e.candidate for e in sel.selection.excluded}  # needs a season


def test_predictor_dates_points_and_keeps_bounds_ordered() -> None:
    model = Naive().fit(np.array([1.0, 2.0, 3.0]))
    points, bounded = forecast_points(model, dt.date(2026, 11, 1), "month", 3, 0.9)
    assert bounded
    assert [p.period for p in points] == ["2026-12-01", "2027-01-01", "2027-02-01"]
    assert next_periods(dt.date(2026, 9, 28), "week", 1) == [dt.date(2026, 10, 5)]
