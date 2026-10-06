"""Trainer + resource guard (TASK-ML-007, TRD 17.2, design 65).

Every fit goes through ``Trainer.fit``:
1. cost estimate = rows x features x catalog ``cost_per_row`` (baselines cost 1 per row);
2. within budget → fit on everything; over budget and the data may be sampled (independent
   rows: anomaly/clustering) → fit on a seeded random sample and record a caveat; over budget
   and not samplable (a time series) → refuse with ``ModelError``;
3. library warnings are captured and reported as a caveat, library failures become
   ``ModelError`` (the tool returns an error result, never a partial model).
"""

import warnings
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from app.core.errors import ModelError
from app.ml.config import ML_CONFIG, MLConfig

Floats = NDArray[np.float64]


@dataclass(frozen=True)
class Fitted[T]:
    model: T
    rows: NDArray[np.intp] | None  # indices actually used; None = all rows
    caveats: list[str]


class Trainer:
    def __init__(self, config: MLConfig = ML_CONFIG) -> None:
        self._cfg = config

    def cost(self, name: str, rows: int, features: int) -> float:
        per_row = next((e.cost_per_row for e in self._cfg.catalog if e.name == name), 1.0)
        return rows * max(features, 1) * per_row

    def fit[T](
        self,
        name: str,
        fit: Callable[[Floats], T],
        x: Floats,
        seed: int,
        *,
        can_downsample: bool,
    ) -> Fitted[T]:
        rows = len(x)
        features = x.shape[1] if x.ndim == 2 else 1
        budget = self._cfg.resources.max_cost_units
        estimate = self.cost(name, rows, features)
        caveats: list[str] = []
        used: NDArray[np.intp] | None = None
        if estimate > budget:
            if not can_downsample:
                raise ModelError(
                    f"{name}: estimated cost {estimate:.3g} exceeds the budget {budget:.3g}; "
                    "time-ordered data cannot be down-sampled, so the fit is refused."
                )
            keep = int(budget // self.cost(name, 1, features))
            if keep < 2:
                raise ModelError(f"{name}: budget allows fewer than 2 rows; fit refused.")
            rng = np.random.default_rng(seed)
            used = np.sort(rng.choice(rows, size=keep, replace=False)).astype(np.intp)
            x = x[used]
            caveats.append(
                f"{name} was fitted on a seeded random sample of {keep} of {rows} rows "
                "(resource budget)."
            )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                model = fit(x)
            except (ValueError, ArithmeticError, np.linalg.LinAlgError) as exc:
                raise ModelError(f"{name} could not be fitted: {type(exc).__name__}") from exc
        if caught:
            caveats.append(f"{name} emitted {len(caught)} fitting warning(s) (e.g. convergence).")
        return Fitted(model=model, rows=used, caveats=caveats)
