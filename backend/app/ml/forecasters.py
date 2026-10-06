"""Forecasters behind one interface (TASK-ML-005 baselines, TASK-ML-012 candidates; ADR-0017).

``fit(y)`` then ``predict(h, level) -> PointAndIntervals``. Baselines (naive, seasonal-naive)
are in-house with the textbook normal intervals (Hyndman & Athanasopoulos, fpp3 5.5):
naive sd_h = sigma·√h, seasonal-naive sd_h = sigma·√(⌊(h-1)/m⌋+1). Candidates wrap statsmodels ETS
(additive error, so intervals are analytical, not simulated), SARIMAX with a small AIC grid,
and STL + ETS. All fits are deterministic (no random initialisation).
"""

import itertools
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any, Protocol, Self

import numpy as np
import pandas as pd
import statsmodels
from numpy.typing import NDArray
from pydantic import JsonValue
from statsmodels.tsa.exponential_smoothing.ets import ETSModel
from statsmodels.tsa.forecasting.stl import STLForecast
from statsmodels.tsa.statespace.sarimax import SARIMAX

from app import __version__

Floats = NDArray[np.float64]
STATSMODELS = f"statsmodels@{statsmodels.__version__}"
IN_HOUSE = f"analyzer@{__version__}"


@dataclass(frozen=True)
class PointAndIntervals:
    mean: Floats
    lower: Floats
    upper: Floats


class Forecaster(Protocol):
    name: str
    library: str
    params: dict[str, JsonValue]

    def fit(self, y: Floats) -> Self: ...
    def predict(self, h: int, level: float) -> PointAndIntervals: ...


def _z(level: float) -> float:
    return NormalDist().inv_cdf(0.5 + level / 2)


class Naive:
    name, library = "naive", IN_HOUSE

    def __init__(self) -> None:
        self.params: dict[str, JsonValue] = {}
        self._last = 0.0
        self._sigma = 0.0

    def fit(self, y: Floats) -> Self:
        self._last = float(y[-1])
        resid = np.diff(y)
        self._sigma = float(np.sqrt(np.mean(resid**2))) if len(resid) else 0.0
        return self

    def predict(self, h: int, level: float) -> PointAndIntervals:
        mean = np.full(h, self._last)
        half = _z(level) * self._sigma * np.sqrt(np.arange(1, h + 1))
        return PointAndIntervals(mean, mean - half, mean + half)


class SeasonalNaive:
    name, library = "seasonal_naive", IN_HOUSE

    def __init__(self, season: int) -> None:
        self.params: dict[str, JsonValue] = {"season": season}
        self._m = season
        self._tail: Floats = np.zeros(0)
        self._sigma = 0.0

    def fit(self, y: Floats) -> Self:
        if len(y) <= self._m:
            raise ValueError("seasonal-naive needs more than one season of history")
        self._tail = y[-self._m :]
        resid = y[self._m :] - y[: -self._m]
        self._sigma = float(np.sqrt(np.mean(resid**2)))
        return self

    def predict(self, h: int, level: float) -> PointAndIntervals:
        steps = np.arange(h)
        mean = self._tail[steps % self._m].astype(np.float64)
        half = _z(level) * self._sigma * np.sqrt(steps // self._m + 1)
        return PointAndIntervals(mean, mean - half, mean + half)


def _frame(pred: Any, cols: tuple[str, str, str]) -> PointAndIntervals:
    mean, low, high = (pred[c].to_numpy(dtype=np.float64) for c in cols)
    return PointAndIntervals(mean, low, high)


class Ets:
    name, library = "ets", STATSMODELS

    def __init__(self, season: int | None) -> None:
        self._season = season
        self.params: dict[str, JsonValue] = {}
        self._result: Any = None
        self._n = 0

    def fit(self, y: Floats) -> Self:
        seasonal = self._season if self._season and len(y) >= 2 * self._season else None
        self.params = {
            "error": "add",
            "trend": "add",
            "damped_trend": True,
            "seasonal": "add" if seasonal else None,
            "seasonal_periods": seasonal,
        }
        model = ETSModel(
            pd.Series(y),
            error="add",
            trend="add",
            damped_trend=True,
            seasonal="add" if seasonal else None,
            seasonal_periods=seasonal,
        )
        self._result, self._n = model.fit(disp=False), len(y)
        return self

    def predict(self, h: int, level: float) -> PointAndIntervals:
        pred = self._result.get_prediction(start=self._n, end=self._n + h - 1)
        return _frame(pred.summary_frame(alpha=1 - level), ("mean", "pi_lower", "pi_upper"))


class Arima:
    """SARIMAX with the lowest AIC over p, d, q ∈ {0,1} (RES-008 rec. 4)."""

    name, library = "arima", STATSMODELS

    def __init__(self, season: int | None) -> None:
        self._season = season
        self.params: dict[str, JsonValue] = {}
        self._result: Any = None

    def fit(self, y: Floats) -> Self:
        m = self._season if self._season and len(y) >= 2 * self._season + 2 else None
        seasonal = (0, 1, 1, m) if m else (0, 0, 0, 0)
        best: tuple[float, Any, tuple[int, ...]] | None = None
        for order in itertools.product((0, 1), (0, 1), (0, 1)):
            trend = "c" if order[1] == 0 and not m else "n"
            try:
                res = SARIMAX(y, order=order, seasonal_order=seasonal, trend=trend).fit(disp=False)
            except np.linalg.LinAlgError, ValueError:
                continue
            aic = float(res.aic)
            if np.isfinite(aic) and (best is None or aic < best[0]):
                best = (aic, res, order)
        if best is None:
            raise ValueError("no ARIMA order could be fitted")
        self._result = best[1]
        self.params = {"order": list(best[2]), "seasonal_order": list(seasonal), "aic": best[0]}
        return self

    def predict(self, h: int, level: float) -> PointAndIntervals:
        fc = self._result.get_forecast(h)
        ci = np.asarray(fc.conf_int(alpha=1 - level), dtype=np.float64)
        mean = np.asarray(fc.predicted_mean, dtype=np.float64)
        return PointAndIntervals(mean, ci[:, 0], ci[:, 1])


class StlEts:
    name, library = "stl_ets", STATSMODELS

    def __init__(self, season: int) -> None:
        self.params: dict[str, JsonValue] = {"period": season, "robust": True, "model": "ets"}
        self._m = season
        self._result: Any = None
        self._n = 0

    def fit(self, y: Floats) -> Self:
        model = STLForecast(
            pd.Series(y),
            ETSModel,
            model_kwargs={"error": "add", "trend": "add", "damped_trend": True},
            period=self._m,
            robust=True,
        )
        self._result, self._n = model.fit(fit_kwargs={"disp": False}), len(y)
        return self

    def predict(self, h: int, level: float) -> PointAndIntervals:
        pred = self._result.get_prediction(start=self._n, end=self._n + h - 1)
        cols = ("mean", "mean_ci_lower", "mean_ci_upper")
        return _frame(pred.summary_frame(alpha=1 - level), cols)


def make_forecaster(name: str, season: int | None) -> Forecaster:
    if name == "naive":
        return Naive()
    if name == "seasonal_naive" and season:
        return SeasonalNaive(season)
    if name == "ets":
        return Ets(season)
    if name == "arima":
        return Arima(season)
    if name == "stl_ets" and season:
        return StlEts(season)
    raise ValueError(f"unknown forecaster {name!r} (season={season})")
