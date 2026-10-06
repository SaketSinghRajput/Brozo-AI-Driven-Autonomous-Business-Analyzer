"""Anomaly detectors (TASK-ML-015..018, design 26). Pure functions over arrays.

Each detector returns a ``Detection``: a signed statistic and an expected value per point, the
threshold, and which points exceed it. The statistic is method-specific:
- robust z (ML-015): modified z-score 0.6745·(x - median)/MAD (Iglewicz & Hoaglin; |z| > 3.5);
- IQR (ML-015): distance outside [Q1, Q3] in IQR units; flagged beyond k = 1.5 (Tukey fences);
- STL residual (ML-016): robust z of the residual of a robust STL fit; expected = trend+season;
- contextual (ML-017): robust z within each member of a dimension (the member's own baseline);
- Isolation Forest (ML-018): seeded ``sklearn.ensemble.IsolationForest``; statistic = -score
  (higher = more isolated); flagged = the ``contamination`` share, an explicit recorded
  assumption (the forest flags roughly that share by construction).
"""

from dataclasses import dataclass

import numpy as np
import sklearn
from numpy.typing import NDArray
from sklearn.ensemble import IsolationForest

from app.ml.timeseries import robust_z, stl_parts

Floats = NDArray[np.float64]
SKLEARN = f"scikit-learn@{sklearn.__version__}"


@dataclass(frozen=True)
class Detection:
    statistic: Floats  # signed; sign gives the direction (high/low)
    expected: Floats | None
    threshold: float
    flagged: NDArray[np.bool_]


def by_robust_z(values: Floats, threshold: float) -> Detection:
    z = robust_z(values)
    expected = np.full(len(values), float(np.median(values)))
    return Detection(z, expected, threshold, np.abs(z) > threshold)


def by_iqr(values: Floats, k: float) -> Detection:
    q1, q3 = (float(q) for q in np.percentile(values, [25, 75]))
    iqr = q3 - q1
    if iqr == 0:  # same fallback scale as robust_z
        iqr = 1.2533 * float(np.mean(np.abs(values - np.median(values)))) or 1.0
    outside = values - np.clip(values, q1, q3)
    stat = np.asarray(outside / iqr, dtype=np.float64)
    expected = np.full(len(values), float(np.median(values)))
    return Detection(stat, expected, k, np.abs(stat) > k)


def by_stl_residual(values: Floats, season: int, threshold: float) -> Detection:
    expected, resid = stl_parts(values, season)
    z = robust_z(resid)
    return Detection(z, expected, threshold, np.abs(z) > threshold)


def by_context(values: Floats, groups: NDArray[np.str_], threshold: float) -> Detection:
    z = np.zeros(len(values))
    expected = np.zeros(len(values))
    for member in np.unique(groups):
        idx = np.flatnonzero(groups == member)
        z[idx] = robust_z(values[idx])
        expected[idx] = float(np.median(values[idx]))
    return Detection(z, expected, threshold, np.abs(z) > threshold)


def by_peer_ratio(matrix: Floats, threshold: float) -> Detection:
    """Contextual over time: ``matrix[t, g]`` = member g's value in period t (NaN = no rows).

    Each member is first indexed to its own median level (n = value / member median). A cell
    is then compared with its peers in the same period: r = n / median(n of the other members),
    and flagged by robust z of r within the member over time. A shared trend or seasonality
    cancels out, and one member's collapse or spike does not move the others' baseline (median
    of indexed peers). Arrays are flattened row-major (period, member); NaN cells never flag.
    """
    periods, members = matrix.shape
    stat = np.zeros((periods, members))
    expected = np.full((periods, members), np.nan)
    level = np.nanmedian(matrix, axis=0)
    indexed = matrix / np.where(level == 0, np.nan, level)
    for g in range(members):
        peers = np.nanmedian(np.delete(indexed, g, axis=1), axis=1)
        ratio = indexed[:, g] / np.where(peers == 0, np.nan, peers)
        ok = np.isfinite(ratio)
        if ok.sum() < 3:
            continue
        stat[ok, g] = robust_z(ratio[ok])
        expected[ok, g] = float(np.median(ratio[ok])) * peers[ok] * level[g]
    flat = stat.reshape(-1)
    return Detection(flat, expected.reshape(-1), threshold, np.abs(flat) > threshold)


def by_isolation_forest(
    x: Floats, contamination: float, seed: int, trees: int, max_samples: int
) -> tuple[Detection, IsolationForest]:
    """``max_samples`` well above sklearn's 256: with 256 the trees rarely contain a lone
    extreme row, so it is not isolated (measured on the retail fixture; docs/ML-models.md)."""
    forest = IsolationForest(
        n_estimators=trees,
        contamination=contamination,
        max_samples=min(len(x), max_samples),
        random_state=seed,
    ).fit(x)
    stat = -np.asarray(forest.score_samples(x), dtype=np.float64)
    flagged = np.asarray(forest.predict(x) == -1)
    threshold = float(-forest.offset_)
    return Detection(stat, None, threshold, flagged), forest


def feature_deviation(x: Floats, rows: NDArray[np.intp]) -> Floats:
    """Mean |robust z| of each feature over the given rows (contributing-feature ranking)."""
    z = np.column_stack([robust_z(x[:, j]) for j in range(x.shape[1])])
    return np.asarray(np.abs(z[rows]).mean(axis=0), dtype=np.float64)


def display_scores(stat: Floats) -> Floats:
    """|statistic| scaled to [0, 1] by the largest one (display ordering only)."""
    mags = np.abs(stat)
    top = float(mags.max()) if len(mags) else 0.0
    return mags / top if top > 0 else np.zeros_like(mags)
