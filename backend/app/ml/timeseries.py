"""Time-series preparation (TASK-ML-010 detectors, TASK-ML-011 preprocessor; design 25).

- ``build_series``: TimeSeriesDetector. Aggregates rows to one value per calendar period of
  the chosen grain (default: the native grain when it is weekly or coarser, else
  ``default_grain``), lays them on a full calendar and marks periods without rows as missing
  (NaN) — never as zero.
- ``detect_season``: SeasonalityDetector. Candidate period from the grain (7/52/12/4), kept
  only when there are ``min_seasonal_cycles`` full cycles and the STL seasonal strength
  F_s = max(0, 1 - Var(R) / Var(S + R)) (Hyndman & Athanasopoulos, fpp3 4.3) reaches the
  configured minimum; otherwise ``None``.
- ``prepare``: ForecastPreprocessor. Explicit fill policy (``interpolate`` default, ``zero``
  only on request, ``refuse`` = no filling), outliers flagged (robust z of the deviation from a
  centred rolling median) but never removed.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import numpy as np
import polars as pl
from numpy.typing import NDArray
from statsmodels.tsa.seasonal import STL

from app.core.errors import DataError
from app.data.profiling.temporal import temporal_stats
from app.ml.config import ML_CONFIG, ForecastConfig, ForecastGrain

Floats = NDArray[np.float64]
Agg = Literal["sum", "mean", "count"]
FillPolicy = Literal["interpolate", "zero", "refuse"]
EVERY: dict[ForecastGrain, str] = {
    "day": "1d",
    "week": "1w",
    "month": "1mo",
    "quarter": "1q",
    "year": "1y",
}


@dataclass(frozen=True)
class TimeSeries:
    periods: list[dt.date]
    values: Floats  # NaN = period without rows
    grain: ForecastGrain
    native_grain: str | None
    regularity: float | None  # of the raw timestamps; only when grain == native grain
    rows: int

    @property
    def missing_ratio(self) -> float:
        return float(np.isnan(self.values).mean()) if len(self.values) else 0.0


def as_dates(frame: pl.DataFrame, column: str) -> pl.Series:
    if column not in frame.columns:
        raise DataError(f"unknown column {column!r}")
    col = frame[column]
    if col.dtype == pl.String:
        col = col.str.to_date(strict=False)
    if isinstance(col.dtype, pl.Datetime):
        col = col.dt.date()
    if col.dtype.is_integer() and col.drop_nulls().is_between(1000, 9999).all():
        # B-1: a four-digit date-part year column (agriculture ``year``) is a yearly series, as
        # in app.planner.facts; the capability thresholds decide whether it is long enough.
        col = frame.select(pl.date(pl.col(column), 1, 1)).to_series()
    if col.dtype != pl.Date:
        raise DataError(f"column {column!r} is not a date")
    return col


def row_values(frame: pl.DataFrame, metric: str | None, multiply_by: list[str]) -> pl.Series:
    """One value per row: metric x each multiply_by column, or 1 per row for a count."""
    for name in ([metric] if metric else []) + multiply_by:
        if name not in frame.columns or not frame[name].dtype.is_numeric():
            raise DataError(f"column {name!r} is not a numeric column")
    if metric is None:
        return pl.Series("value", np.ones(frame.height))
    value = frame[metric].cast(pl.Float64)
    for name in multiply_by:
        value = value * frame[name].cast(pl.Float64)
    return value.alias("value")


def choose_grain(native: str | None, requested: ForecastGrain | None) -> ForecastGrain:
    if requested is not None:
        return requested
    if native in ("week", "month", "quarter", "year"):
        return native  # type: ignore[return-value]  # narrowed by the membership test
    return ML_CONFIG.forecast.default_grain


def build_series(
    frame: pl.DataFrame,
    time_column: str,
    metric: str | None,
    multiply_by: list[str],
    agg: Agg,
    grain: ForecastGrain | None,
) -> TimeSeries:
    dates = as_dates(frame, time_column)
    stats = temporal_stats(dates)
    native = stats.granularity if stats else None
    chosen = choose_grain(native, grain)
    data = pl.DataFrame({"t": dates, "value": row_values(frame, metric, multiply_by)})
    data = data.drop_nulls()
    if data.height == 0:
        return TimeSeries([], np.zeros(0), chosen, native, None, 0)
    data = data.with_columns(pl.col("t").dt.truncate(EVERY[chosen]).alias("period"))
    reducer = {"sum": pl.col("value").sum(), "mean": pl.col("value").mean(), "count": pl.len()}
    grouped = data.group_by("period").agg(reducer[agg].cast(pl.Float64).alias("value"))
    first, last = grouped["period"].min(), grouped["period"].max()
    if not (isinstance(first, dt.date) and isinstance(last, dt.date)):
        raise DataError(f"column {time_column!r} has no dates")
    calendar = pl.date_range(first, last, EVERY[chosen], eager=True).alias("period")
    full = calendar.to_frame().join(grouped, on="period", how="left").sort("period")
    return TimeSeries(
        periods=full["period"].to_list(),
        values=full["value"].fill_null(np.nan).to_numpy().astype(np.float64),
        grain=chosen,
        native_grain=native,
        regularity=stats.regularity if stats and native == chosen else None,
        rows=data.height,
    )


def next_periods(last: dt.date, grain: ForecastGrain, h: int) -> list[dt.date]:
    end = last + dt.timedelta(days=370 * (h + 1))
    periods: list[dt.date] = pl.date_range(last, end, EVERY[grain], eager=True).to_list()
    return periods[1 : h + 1]


def stl_parts(y: Floats, season: int) -> tuple[Floats, Floats]:
    """(trend + seasonal, residual) of a robust STL fit."""
    fit = STL(y, period=season, robust=True).fit()
    expected = np.asarray(fit.trend + fit.seasonal, dtype=np.float64)
    return expected, np.asarray(fit.resid, dtype=np.float64)


def detect_season(
    y: Floats, grain: ForecastGrain, cfg: ForecastConfig = ML_CONFIG.forecast
) -> tuple[int | None, float | None]:
    """(season or None, seasonal strength or None when it could not be measured)."""
    m = cfg.season_by_grain.get(grain)
    if m is None or len(y) < cfg.min_seasonal_cycles * m:
        return None, None
    fit = STL(y, period=m, robust=True).fit()
    denom = float(np.var(fit.seasonal + fit.resid))
    strength = 0.0 if denom == 0 else max(0.0, 1 - float(np.var(fit.resid)) / denom)
    return (m if strength >= cfg.min_seasonal_strength else None), strength


def robust_z(values: Floats) -> Floats:
    """Modified z = 0.6745·(x - median)/MAD; if MAD = 0, (x - median)/(1.2533·mean|x - median|)."""
    dev = values - np.median(values)
    mad = float(np.median(np.abs(dev)))
    if mad > 0:
        return np.asarray(0.6745 * dev / mad, dtype=np.float64)
    mean_ad = float(np.mean(np.abs(dev)))
    if mean_ad == 0:
        return np.zeros_like(values, dtype=np.float64)
    return np.asarray(dev / (1.2533 * mean_ad), dtype=np.float64)


@dataclass(frozen=True)
class Prepared:
    y: Floats
    filled: int
    outliers: list[int]  # flagged period indices (kept in the data)
    notes: list[str]


def prepare(ts: TimeSeries, fill: FillPolicy, outlier_z: float) -> Prepared:
    y = ts.values.copy()
    gaps = np.isnan(y)
    notes: list[str] = []
    if gaps.any():
        if fill == "refuse":
            raise DataError(f"{int(gaps.sum())} missing {ts.grain} periods and fill='refuse'")
        if fill == "zero":
            y[gaps] = 0.0
            notes.append(f"{int(gaps.sum())} missing {ts.grain} periods set to 0 (requested).")
        else:
            idx = np.arange(len(y))
            y[gaps] = np.interp(idx[gaps], idx[~gaps], y[~gaps])
            notes.append(
                f"{int(gaps.sum())} missing {ts.grain} periods filled by linear interpolation "
                "(policy 'interpolate'; never silently zero)."
            )
    padded = np.pad(y, 2, mode="edge")
    rolling = np.median(np.lib.stride_tricks.sliding_window_view(padded, 5), axis=1)
    z = robust_z(y - rolling)
    outliers = [int(i) for i in np.flatnonzero(np.abs(z) > outlier_z)]
    if outliers:
        notes.append(f"{len(outliers)} outlier period(s) flagged and kept (not removed).")
    return Prepared(y=y, filled=int(gaps.sum()), outliers=outliers, notes=notes)
