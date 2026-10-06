"""Predictor (TASK-ML-009, design 23): fitted model → contract predictions, deterministically.

Turns a fitted forecaster's output into ``ForecastPoint``s dated by period start. Intervals are
kept only when every bound is finite; otherwise the points carry no bounds and the caller must
say why (``ForecastResult`` enforces the note).
"""

import datetime as dt

import numpy as np

from app.ml.config import ForecastGrain
from app.ml.domain import ForecastPoint
from app.ml.forecasters import Forecaster
from app.ml.timeseries import next_periods

NO_INTERVALS_NOTE = "The model returned no finite prediction intervals for this horizon."


def forecast_points(
    model: Forecaster, last: dt.date, grain: ForecastGrain, h: int, level: float
) -> tuple[list[ForecastPoint], bool]:
    """(points, has_intervals)."""
    pred = model.predict(h, level)
    periods = next_periods(last, grain, h)
    bounded = bool(np.all(np.isfinite(pred.lower)) and np.all(np.isfinite(pred.upper)))
    points = []
    for i, period in enumerate(periods):
        value = float(pred.mean[i])
        if not bounded:
            points.append(ForecastPoint(period=period.isoformat(), value=value))
            continue
        low = min(float(pred.lower[i]), value)
        high = max(float(pred.upper[i]), value)
        points.append(ForecastPoint(period=period.isoformat(), value=value, lower=low, upper=high))
    return points, bounded
