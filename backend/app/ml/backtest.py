"""Rolling-origin ForecastEvaluator (TASK-ML-013, design 25, TRD 18).

Each candidate is refitted on every expanding training window of the same temporal splits and
scored on the next ``horizon`` points. Metrics come from ``evaluator.forecast_metrics`` per fold
and are averaged over folds (MASE uses each fold's own training window for its scale). The fold
MAEs are kept for the selector's consistency rule. Deterministic: same series → same numbers.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from app.ml.evaluator import forecast_metrics, mase_scale
from app.ml.forecasters import make_forecaster
from app.ml.splitter import Split
from app.ml.trainer import Trainer

Floats = NDArray[np.float64]


@dataclass(frozen=True)
class Backtest:
    name: str
    values: dict[str, float]  # fold-averaged metrics
    fold_mae: list[float]
    caveats: list[str]


def backtest(
    name: str,
    season: int | None,
    y: Floats,
    splits: list[Split],
    level: float,
    trainer: Trainer,
    seed: int,
) -> Backtest:
    """Raises ``ModelError`` when the candidate cannot be fitted on some window."""
    per_fold: list[dict[str, float]] = []
    caveats: list[str] = []
    for split in splits:
        train, test = y[split.train], y[split.test]
        fitted = trainer.fit(
            name,
            lambda x: make_forecaster(name, season).fit(x),
            train,
            seed,
            can_downsample=False,
        )
        caveats += [c for c in fitted.caveats if c not in caveats]
        pred = fitted.model.predict(len(test), level)
        per_fold.append(
            forecast_metrics(test, pred.mean, mase_scale(train, season), pred.lower, pred.upper)
        )
    keys = set.intersection(*(set(m) for m in per_fold))
    values = {k: float(np.mean([m[k] for m in per_fold])) for k in sorted(keys)}
    return Backtest(name, values, [m["mae"] for m in per_fold], caveats)
