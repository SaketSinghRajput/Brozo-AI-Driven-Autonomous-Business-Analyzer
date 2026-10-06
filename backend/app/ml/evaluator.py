"""Evaluator (TASK-ML-008, design 23, TRD 18): the only place metrics are computed.

Definitions (also in docs/ML-models.md):
- MAE = mean|y - ŷ|; RMSE = sqrt(mean (y - ŷ)²)
- sMAPE = mean(2|y - ŷ| / (|y| + |ŷ|)) as a fraction in [0, 2]; a term with y = ŷ = 0 counts 0
- MASE = MAE / in-sample MAE of the (seasonal-)naive one-step forecast on the training window;
  omitted when that scale is 0 (constant history), never reported as infinity
- coverage = share of actuals inside [lower, upper]
- supervised: accuracy + macro F1 (classification); MAE, RMSE, R² (regression)
- clustering: silhouette (seeded sample above ``sample`` rows) and Davies-Bouldin
"""

from collections.abc import Callable
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray
from sklearn.metrics import davies_bouldin_score, f1_score, r2_score, silhouette_score

from app.ml.domain import BaselineScore, Metrics
from app.ml.splitter import Split

Floats = NDArray[np.float64]


def mase_scale(train: Floats, season: int | None) -> float:
    lag = season if season and len(train) > season else 1
    if len(train) <= lag:
        return 0.0
    return float(np.mean(np.abs(train[lag:] - train[:-lag])))


def forecast_metrics(
    actual: Floats,
    pred: Floats,
    scale: float,
    lower: Floats | None = None,
    upper: Floats | None = None,
) -> dict[str, float]:
    err = actual - pred
    denom = np.abs(actual) + np.abs(pred)
    safe = np.where(denom == 0, 1.0, denom)
    out = {
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "smape": float(np.mean(np.where(denom == 0, 0.0, 2 * np.abs(err) / safe))),
    }
    if scale > 0:
        out["mase"] = out["mae"] / scale
    if lower is not None and upper is not None:
        out["coverage"] = float(np.mean((actual >= lower) & (actual <= upper)))
    return out


def classification_metrics(y: NDArray[np.generic], pred: NDArray[np.generic]) -> dict[str, float]:
    return {
        "accuracy": float(np.mean(y == pred)),
        "f1_macro": float(f1_score(y, pred, average="macro", zero_division=0)),
    }


def regression_metrics(y: Floats, pred: Floats) -> dict[str, float]:
    err = y - pred
    return {
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "r2": float(r2_score(y, pred)) if len(y) > 1 else 0.0,
    }


def clustering_metrics(
    x: Floats, labels: NDArray[np.intp], seed: int, sample: int
) -> dict[str, float]:
    size = sample if len(x) > sample else None
    return {
        "silhouette": float(silhouette_score(x, labels, sample_size=size, random_state=seed)),
        "davies_bouldin": float(davies_bouldin_score(x, labels)),
    }


class Estimator(Protocol):
    def fit(self, x: Floats, y: NDArray[np.generic]) -> object: ...
    def predict(self, x: Floats) -> NDArray[np.generic]: ...


def score_supervised(
    task: Literal["classification", "regression"],
    make_model: Callable[[], Estimator],
    make_baseline: Callable[[], Estimator],
    baseline_name: str,
    x: Floats,
    y: NDArray[np.generic],
    splits: list[Split],
) -> Metrics:
    """Cross-validated metrics of a model and its baseline on the same splits (pooled)."""
    primary = "f1_macro" if task == "classification" else "mae"

    def pooled(make: Callable[[], Estimator]) -> dict[str, float]:
        preds: list[NDArray[np.generic]] = []
        for split in splits:
            model = make()
            model.fit(x[split.train], y[split.train])
            preds.append(model.predict(x[split.test]))
        truth = np.concatenate([y[s.test] for s in splits])
        pred = np.concatenate(preds)
        if task == "classification":
            return classification_metrics(truth, pred)
        return regression_metrics(truth.astype(np.float64), pred.astype(np.float64))

    return Metrics(
        primary=primary,
        values=pooled(make_model),
        baseline=BaselineScore(name=baseline_name, values=pooled(make_baseline)),
    )
