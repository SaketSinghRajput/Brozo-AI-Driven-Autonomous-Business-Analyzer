"""DatasetSplitter (TASK-ML-004, design 23): temporal, k-fold and stratified k-fold; seeded.

Splits are index arrays over rows that are already in time order (temporal) or in any order
(k-fold). Temporal splits are expanding-window rolling origin: every training index is strictly
before every test index of the same fold, so no future value can reach a fit.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from sklearn.model_selection import KFold, StratifiedKFold

Index = NDArray[np.intp]


@dataclass(frozen=True)
class Split:
    train: Index
    test: Index


def temporal(n: int, folds: int, horizon: int, min_train: int) -> list[Split]:
    """Up to ``folds`` rolling-origin splits with non-overlapping test windows of ``horizon``.

    The last fold ends at the last observation; fewer folds are returned when the history
    cannot hold ``min_train`` points before the first origin.
    """
    usable = min(folds, (n - min_train) // horizon) if horizon > 0 else 0
    splits = []
    for i in range(max(usable, 0)):
        origin = n - (usable - i) * horizon
        splits.append(
            Split(
                train=np.arange(origin, dtype=np.intp),
                test=np.arange(origin, origin + horizon, dtype=np.intp),
            )
        )
    return splits


def kfold(n: int, k: int, seed: int) -> list[Split]:
    splitter = KFold(n_splits=k, shuffle=True, random_state=seed)
    return [Split(np.asarray(tr), np.asarray(te)) for tr, te in splitter.split(np.zeros(n))]


def stratified(y: NDArray[np.generic], k: int, seed: int) -> list[Split]:
    splitter = StratifiedKFold(n_splits=k, shuffle=True, random_state=seed)
    return [Split(np.asarray(tr), np.asarray(te)) for tr, te in splitter.split(np.zeros(len(y)), y)]
