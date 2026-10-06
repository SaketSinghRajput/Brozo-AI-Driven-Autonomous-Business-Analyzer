"""Baselines that always run (TASK-ML-005, TRD 17.1).

Forecasting baselines (naive, seasonal-naive) live with the other forecasters in
``forecasters.py``. This module holds the supervised ones — majority class, mean, and ordinary
least-squares linear regression — plus the rule for which baseline a forecast uses.
"""

from typing import Self

import numpy as np
from numpy.typing import NDArray

Floats = NDArray[np.float64]


class MajorityClass:
    name = "majority"

    def __init__(self) -> None:
        self._label: object = None

    def fit(self, x: Floats, y: NDArray[np.generic]) -> Self:
        values, counts = np.unique(y, return_counts=True)
        self._label = values[int(np.argmax(counts))]  # ties -> smallest label (sorted)
        return self

    def predict(self, x: Floats) -> NDArray[np.generic]:
        return np.full(len(x), self._label)


class MeanRegressor:
    name = "mean"

    def __init__(self) -> None:
        self._mean = 0.0

    def fit(self, x: Floats, y: NDArray[np.generic]) -> Self:
        self._mean = float(np.mean(y.astype(np.float64)))
        return self

    def predict(self, x: Floats) -> NDArray[np.generic]:
        return np.full(len(x), self._mean)


class LinearRegressor:
    name = "linear"

    def __init__(self) -> None:
        self._coef: Floats = np.zeros(0)

    def fit(self, x: Floats, y: NDArray[np.generic]) -> Self:
        design = np.column_stack([np.ones(len(x)), x])
        self._coef = np.linalg.lstsq(design, y.astype(np.float64), rcond=None)[0]
        return self

    def predict(self, x: Floats) -> NDArray[np.generic]:
        result: NDArray[np.generic] = np.column_stack([np.ones(len(x)), x]) @ self._coef
        return result


def forecast_baseline(season: int | None) -> str:
    """Seasonal-naive when a significant season was found, else naive (design 25)."""
    return "seasonal_naive" if season else "naive"
