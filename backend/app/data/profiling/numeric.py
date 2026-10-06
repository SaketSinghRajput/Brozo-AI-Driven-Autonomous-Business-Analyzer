"""StatisticalProfiler math (TASK-PROFILE-003/004/006): numeric stats, outliers, correlations.

Definitions (fixed, so hand-computed fixtures can check them):
- std/var: sample (ddof=1); 0.0 when fewer than two values.
- skew: adjusted Fisher-Pearson (bias=False, as pandas/scipy `bias=False`); None when n < 3
  or std == 0.
- quantiles: linear interpolation (numpy default) at 1/5/25/50/75/95/99 %.
- outliers: IQR fences (Q1 - 1.5 IQR, Q3 + 1.5 IQR); when IQR == 0, modified z-score
  0.6745 (x - median) / MAD > 3.5 (Iglewicz-Hoaglin), or (x - median) / (1.253314 meanAD)
  when MAD == 0 as well; a constant column has none.
- correlations: Pearson and Spearman on pairwise-complete rows, n >= 3, both non-constant.
"""

import math
from typing import Any

import polars as pl

from app.data.profiling.domain import CorrelationPair, NumericStats, OutlierSummary

QUANTILES = {
    "q01": 0.01,
    "q05": 0.05,
    "q25": 0.25,
    "q50": 0.5,
    "q75": 0.75,
    "q95": 0.95,
    "q99": 0.99,
}
OUTLIER_SAMPLE = 10
NEAR_DUPLICATE = 0.999


def _f(value: Any) -> float:
    return float(value)


def numeric_stats(series: pl.Series) -> NumericStats | None:
    v = series.drop_nulls().cast(pl.Float64)
    n = v.len()
    if n == 0:
        return None
    std = _f(v.std(ddof=1)) if n > 1 else 0.0
    skew = v.skew(bias=False) if n >= 3 and std > 0 else None
    return NumericStats(
        min=_f(v.min()),
        max=_f(v.max()),
        mean=_f(v.mean()),
        median=_f(v.median()),
        std=std,
        var=std**2,
        skew=None if skew is None or math.isnan(skew) else float(skew),
        quantiles={k: _f(v.quantile(q, "linear")) for k, q in QUANTILES.items()},
        zero_ratio=(v == 0).sum() / n,
        neg_ratio=(v < 0).sum() / n,
    )


def outliers(series: pl.Series) -> OutlierSummary | None:
    """Indices refer to row positions in the stored dataset; values are never included."""
    v = series.cast(pl.Float64)
    present = v.drop_nulls()
    if present.len() == 0:
        return None
    q1, q3 = _f(present.quantile(0.25, "linear")), _f(present.quantile(0.75, "linear"))
    iqr = q3 - q1
    method: Any
    if iqr > 0:
        method = "iqr"
        mask = (v < q1 - 1.5 * iqr) | (v > q3 + 1.5 * iqr)
    else:
        method = "mad"
        median = _f(present.median())
        deviation = (v - median).abs()
        mad = _f((present - median).abs().median())
        mean_ad = _f((present - median).abs().mean())
        if mad > 0:
            mask = deviation * 0.6745 / mad > 3.5
        elif mean_ad > 0:  # MAD == 0 (over half the values equal): mean-absolute-deviation form
            mask = deviation / (1.253314 * mean_ad) > 3.5
        else:
            mask = v.is_nan()  # constant column: no outliers
    hits = mask.fill_null(False).arg_true()
    return OutlierSummary(
        method=method, count=hits.len(), sample_indices=hits.head(OUTLIER_SAMPLE).to_list()
    )


def correlations(frame: pl.DataFrame, columns: list[str]) -> list[CorrelationPair]:
    pairs: list[CorrelationPair] = []
    for i, a in enumerate(columns):
        for b in columns[i + 1 :]:
            both = frame.select(pl.col(a).cast(pl.Float64), pl.col(b).cast(pl.Float64))
            both = both.drop_nulls()
            n = both.height
            if n < 3 or not _f(both[a].std() or 0) or not _f(both[b].std() or 0):
                continue
            for method in ("pearson", "spearman"):
                r = both.select(pl.corr(a, b, method=method)).item()
                if r is not None and not math.isnan(r):
                    coefficient = max(-1.0, min(1.0, float(r)))
                    pairs.append(
                        CorrelationPair(a=a, b=b, method=method, coefficient=coefficient, n=n)
                    )
    return pairs


def near_duplicate_flags(pairs: list[CorrelationPair]) -> list[str]:
    return [
        f"NEAR_DUPLICATE_COLUMNS:{p.a}|{p.b}"
        for p in pairs
        if p.method == "pearson" and abs(p.coefficient) >= NEAR_DUPLICATE
    ]
