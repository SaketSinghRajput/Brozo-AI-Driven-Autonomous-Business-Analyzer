"""Build the evaluated points of ``ml.anomalies`` for each data shape and run the detector.

- ``multivariate_points``: rows x ``features`` → Isolation Forest (via the Trainer, so the
  resource guard can down-sample with a caveat);
- ``series_points``: one value per period (``build_series``) → STL residual when a season is
  found (or requested), else robust z / IQR; with a dimension → peer-ratio per member x period;
- ``row_points``: one value per row → robust z / IQR, or z within each dimension member.
Each returns ``Points`` (values, subjects, detection, always-run robust-z baseline, params,
caveats) or an ``Unavailability`` from the ``MLTaskDetector``.
"""

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import polars as pl
from numpy.typing import NDArray
from pydantic import JsonValue

from app.core.errors import DataError
from app.ml.anomaly import (
    Detection,
    by_context,
    by_iqr,
    by_isolation_forest,
    by_peer_ratio,
    by_robust_z,
    by_stl_residual,
    feature_deviation,
)
from app.ml.config import ForecastGrain, MLConfig
from app.ml.detector import DataCharacteristics, MLTaskDetector
from app.ml.domain import TaskSpec
from app.ml.timeseries import Agg, build_series, detect_season, row_values
from app.ml.trainer import Trainer
from app.planner.domain import ReasonCode, Unavailability

Floats = NDArray[np.float64]
Subject = dict[str, JsonValue]
MAX_MEMBERS = 50


@dataclass(frozen=True)
class SeriesSpec:
    time_column: str
    metric: str | None
    multiply_by: list[str]
    agg: Agg
    grain: ForecastGrain


@dataclass
class Points:
    x: Floats  # (points, columns); one column for univariate shapes
    subjects: list[Subject]
    detection: Detection
    baseline: NDArray[np.bool_]  # robust z on the same values (always run)
    method: str
    params: dict[str, JsonValue] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    contributing: list[str] = field(default_factory=list)


def _strings(frame: pl.DataFrame, column: str) -> NDArray[np.str_]:
    if column not in frame.columns:
        raise DataError(f"unknown column {column!r}")
    return frame[column].cast(pl.String).fill_null("(null)").to_numpy().astype(str)


def _check(cfg: MLConfig, spec: TaskSpec, chars: DataCharacteristics) -> Unavailability | None:
    decision = MLTaskDetector(cfg).detect(None, spec, chars)
    return decision if isinstance(decision, Unavailability) else None


def _univariate(values: Floats, method: str, cfg: MLConfig, threshold: float | None) -> Detection:
    if method == "iqr":
        return by_iqr(values, threshold or cfg.anomaly.iqr_k)
    return by_robust_z(values, threshold or cfg.anomaly.robust_z_threshold)


def multivariate_points(
    cfg: MLConfig,
    spec: TaskSpec,
    frame: pl.DataFrame,
    features: list[str],
    id_column: str | None,
    contamination: float | None,
    seed: int,
) -> Points | Unavailability:
    data = frame.with_row_index("row").drop_nulls(features)
    x = np.column_stack([row_values(data, f, []).to_numpy() for f in features]).astype(np.float64)
    chars = DataCharacteristics(rows=len(x), numeric_features=x.shape[1], multivariate=True)
    if (refused := _check(cfg, spec, chars)) is not None:
        return refused
    a = cfg.anomaly
    share = contamination or a.contamination
    fitted = Trainer(cfg).fit(
        "isolation_forest",
        lambda m: by_isolation_forest(m, share, seed, a.isolation_trees, a.isolation_max_samples),
        x,
        seed,
        can_downsample=True,
    )
    detection = fitted.model[0]
    rows = data["row"].to_numpy()
    ids = _strings(data, id_column) if id_column else None
    if fitted.rows is not None:
        x, rows = x[fitted.rows], rows[fitted.rows]
        ids = None if ids is None else ids[fitted.rows]
    subjects: list[Subject] = []
    for i, r in enumerate(rows):
        subject: Subject = {"row": int(r)}
        if id_column and ids is not None:
            subject[id_column] = str(ids[i])
        subjects.append(subject)
    flagged = np.flatnonzero(detection.flagged)
    ranking = feature_deviation(x, flagged) if len(flagged) else np.zeros(len(features))
    baseline = np.zeros(len(x), dtype=bool)
    for j in range(x.shape[1]):
        baseline |= by_robust_z(x[:, j], a.robust_z_threshold).flagged
    caveats = [
        *fitted.caveats,
        f"Isolation Forest assumes about {share:.1%} of rows are anomalous (contamination); "
        "it flags roughly that share by construction.",
    ]
    return Points(
        x=x,
        subjects=subjects,
        detection=detection,
        baseline=baseline,
        method="isolation_forest",
        params={"contamination": share, "trees": a.isolation_trees, "features": list(features)},
        caveats=caveats,
        contributing=[features[j] for j in np.argsort(-ranking, kind="stable")],
    )


def series_points(
    cfg: MLConfig,
    spec: TaskSpec,
    frame: pl.DataFrame,
    series: SeriesSpec,
    method: str,
    dimension: str | None,
    threshold: float | None,
) -> Points | Unavailability:
    s = series
    ts = build_series(frame, s.time_column, s.metric, s.multiply_by, s.agg, s.grain)
    chars = DataCharacteristics(rows=len(ts.values), has_time=True, grain=ts.grain)
    if (refused := _check(cfg, spec, chars)) is not None:
        return refused
    params: dict[str, JsonValue] = {"grain": ts.grain, "agg": s.agg}
    if dimension is not None:
        return _peer_points(cfg, frame, ts.periods, series, dimension, threshold, params)
    ok = ~np.isnan(ts.values)
    periods = [p for p, keep in zip(ts.periods, ok, strict=True) if keep]
    values = ts.values[ok]
    caveats = []
    if not ok.all():
        caveats.append(f"{int((~ok).sum())} {ts.grain} periods without rows were not evaluated.")
    season, strength = detect_season(values, ts.grain, cfg.forecast)
    if method == "stl_residual" and season is None:
        caveats.append(
            f"No significant seasonality (STL strength {strength or 0.0:.2f}); robust z on the "
            "series was used instead of the STL residual."
        )
        method = "robust_z"
    if method == "stl_residual" and season is not None:
        detection = by_stl_residual(values, season, threshold or cfg.anomaly.robust_z_threshold)
        params |= {"season": season, "seasonal_strength": strength}
    else:
        detection = _univariate(values, method, cfg, threshold)
    return Points(
        x=values.reshape(-1, 1),
        subjects=[{"period": p.isoformat()} for p in periods],
        detection=detection,
        baseline=by_robust_z(values, cfg.anomaly.robust_z_threshold).flagged,
        method=method,
        params=params,
        caveats=caveats,
    )


def _peer_points(
    cfg: MLConfig,
    frame: pl.DataFrame,
    periods: list[dt.date],
    s: SeriesSpec,
    dimension: str,
    threshold: float | None,
    params: dict[str, JsonValue],
) -> Points | Unavailability:
    labels = _strings(frame, dimension)
    members = sorted(set(labels.tolist()))
    if not 2 <= len(members) <= MAX_MEMBERS:
        return Unavailability(
            analysis="anomaly_detection",
            reason_code=ReasonCode.NO_DIMENSION,
            message=f"Contextual detection needs 2 to {MAX_MEMBERS} members of {dimension!r}; "
            f"found {len(members)}.",
        )
    index = {p: i for i, p in enumerate(periods)}
    matrix = np.full((len(periods), len(members)), np.nan)
    for g, member in enumerate(members):
        part = frame.filter(pl.Series(labels == member))
        ts = build_series(part, s.time_column, s.metric, s.multiply_by, s.agg, s.grain)
        for p, v in zip(ts.periods, ts.values, strict=True):
            matrix[index[p], g] = v
    detection = by_peer_ratio(matrix, threshold or cfg.anomaly.robust_z_threshold)
    values = matrix.reshape(-1)
    ok = np.isfinite(values)
    baseline = np.zeros(len(values), dtype=bool)
    baseline[ok] = by_robust_z(values[ok], cfg.anomaly.robust_z_threshold).flagged
    return Points(
        x=np.nan_to_num(values).reshape(-1, 1),
        subjects=[{"period": p.isoformat(), dimension: m} for p in periods for m in members],
        detection=detection,
        baseline=baseline,
        method="contextual",
        params=params | {"dimension": dimension, "baseline": "peer_median_ratio"},
        caveats=["Each member is compared with the median of the other members per period."],
    )


def row_points(
    cfg: MLConfig,
    spec: TaskSpec,
    frame: pl.DataFrame,
    measure: tuple[str, list[str]],
    method: str,
    dimension: str | None,
    id_column: str | None,
    threshold: float | None,
) -> Points | Unavailability:
    metric, multiply_by = measure
    data = frame.with_row_index("row").with_columns(row_values(frame, metric, multiply_by))
    data = data.drop_nulls("value")
    values = data["value"].to_numpy().astype(np.float64)
    if (refused := _check(cfg, spec, DataCharacteristics(rows=len(values)))) is not None:
        return refused
    labels = _strings(data, dimension) if dimension else None
    ids = _strings(data, id_column) if id_column else None
    if method == "contextual" and labels is not None:
        detection = by_context(values, labels, threshold or cfg.anomaly.robust_z_threshold)
    else:
        detection = _univariate(values, method, cfg, threshold)
    subjects: list[Subject] = []
    for i, r in enumerate(data["row"].to_list()):
        subject: Subject = {"row": int(r)}
        if id_column and ids is not None:
            subject[id_column] = str(ids[i])
        if dimension and labels is not None:
            subject[dimension] = str(labels[i])
        subjects.append(subject)
    return Points(
        x=values.reshape(-1, 1),
        subjects=subjects,
        detection=detection,
        baseline=by_robust_z(values, cfg.anomaly.robust_z_threshold).flagged,
        method=method,
        params={"dimension": dimension} if dimension else {},
    )
