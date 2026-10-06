"""FeatureBuilder (TASK-ML-003, design 23): encoders, scalers and lags, fitted on train only.

``FeatureBuilder.fit(train)`` learns every statistic (numeric medians for imputation, means and
standard deviations for scaling, categorical vocabularies for one-hot encoding) from the
training frame alone; ``FittedFeatures.transform`` applies them unchanged to any frame, so test
values can never influence the fitted parameters. Unseen categories encode as all zeros.
``lagged`` builds lag columns from strictly earlier rows of a time-ordered frame.
"""

from dataclasses import dataclass

import numpy as np
import polars as pl
from numpy.typing import NDArray

from app.core.errors import DataError

Floats = NDArray[np.float64]


@dataclass(frozen=True)
class FittedFeatures:
    numeric: tuple[str, ...]
    log1p: tuple[str, ...]
    medians: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    vocab: tuple[tuple[str, tuple[str, ...]], ...]  # (column, categories seen in train)

    @property
    def names(self) -> list[str]:
        cats = [f"{col}={v}" for col, values in self.vocab for v in values]
        return [*self.numeric, *cats]

    def _numeric(self, frame: pl.DataFrame) -> Floats:
        cols = []
        for i, name in enumerate(self.numeric):
            raw = frame[name].cast(pl.Float64).fill_null(self.medians[i]).to_numpy()
            values = np.log1p(np.clip(raw, 0, None)) if name in self.log1p else raw
            cols.append((values - self.means[i]) / self.scales[i])
        return np.column_stack(cols) if cols else np.zeros((frame.height, 0))

    def transform(self, frame: pl.DataFrame) -> Floats:
        parts = [self._numeric(frame)]
        for col, values in self.vocab:
            labels = frame[col].cast(pl.String).fill_null("(null)").to_numpy().astype(str)
            parts.append(np.column_stack([labels == v for v in values]).astype(np.float64))
        return np.hstack(parts).astype(np.float64)


class FeatureBuilder:
    def fit(
        self,
        train: pl.DataFrame,
        numeric: list[str],
        categorical: list[str] | None = None,
        log1p: list[str] | None = None,
    ) -> FittedFeatures:
        missing = [c for c in [*numeric, *(categorical or [])] if c not in train.columns]
        if missing:
            raise DataError(f"unknown feature columns {missing}")
        medians, means, scales = [], [], []
        for name in numeric:
            col = train[name].cast(pl.Float64)
            median = col.median()
            fill = float(median) if isinstance(median, int | float) else 0.0
            values = col.fill_null(fill).to_numpy()
            if name in (log1p or []):
                values = np.log1p(np.clip(values, 0, None))
            std = float(np.std(values))
            medians.append(fill)
            means.append(float(np.mean(values)))
            scales.append(std if std > 0 else 1.0)
        vocab = tuple(
            (c, tuple(sorted(set(train[c].cast(pl.String).fill_null("(null)").to_list()))))
            for c in categorical or []
        )
        return FittedFeatures(
            numeric=tuple(numeric),
            log1p=tuple(log1p or []),
            medians=tuple(medians),
            means=tuple(means),
            scales=tuple(scales),
            vocab=vocab,
        )


def lagged(frame: pl.DataFrame, column: str, lags: list[int], order_by: str) -> pl.DataFrame:
    """Add ``<column>_lag<k>`` = value k rows earlier in ``order_by`` order (null at the start)."""
    if any(k < 1 for k in lags):
        raise DataError("lags must be >= 1 (a lag of 0 would leak the current value)")
    ordered = frame.sort(order_by)
    return ordered.with_columns(pl.col(column).shift(k).alias(f"{column}_lag{k}") for k in lags)
