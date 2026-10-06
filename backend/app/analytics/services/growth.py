"""GrowthService (TASK-ANALYSIS-009, design §22, TRD §15.2).

Units follow TRD §25.1: ``pct`` and ``pp`` values are FRACTIONS (-0.142 with unit ``pct`` is
-14.2%; 0.023 with unit ``pp`` is 2.3 percentage points); the UI and the validator display them
x100 (app.validator.numbers).

- percentage change = (x1 - x0) / |x0|, unit ``pct``. x0 = 0 → ``undefined`` (never inf/0).
- inputs that are already percentages (unit ``pct``, fractions) change in percentage points:
  x1 - x0, unit ``pp``.
- CAGR = (x_end / x_start) ** (1 / years) - 1, unit ``pct``; years from actual dates
  (365.2425-day years); requires x_start > 0, x_end >= 0 and a positive duration.
"""

from collections.abc import Sequence
from datetime import date
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict

UndefinedReason = Literal[
    "BASE_IS_ZERO", "MISSING_VALUE", "NON_POSITIVE_START", "NEGATIVE_END", "ZERO_DURATION"
]
DAYS_PER_YEAR = 365.2425


class GrowthResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | None  # None exactly when undefined
    unit: Literal["pct", "pp"]
    base: float | None
    current: float | None
    undefined: UndefinedReason | None = None


class PeriodGrowth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period: str
    growth: GrowthResult


def _undefined(
    reason: UndefinedReason, unit: Literal["pct", "pp"], base: float | None, cur: float | None
) -> GrowthResult:
    return GrowthResult(value=None, unit=unit, base=base, current=cur, undefined=reason)


def pct_change(base: float | None, current: float | None) -> GrowthResult:
    if base is None or current is None:
        return _undefined("MISSING_VALUE", "pct", base, current)
    if base == 0:
        return _undefined("BASE_IS_ZERO", "pct", base, current)
    return GrowthResult(value=(current - base) / abs(base), unit="pct", base=base, current=current)


def pp_change(base: float | None, current: float | None) -> GrowthResult:
    if base is None or current is None:
        return _undefined("MISSING_VALUE", "pp", base, current)
    return GrowthResult(value=current - base, unit="pp", base=base, current=current)


def growth(base: float | None, current: float | None, unit: str | None) -> GrowthResult:
    """Percentage points for percentage-valued metrics, percent change for everything else."""
    return pp_change(base, current) if unit == "pct" else pct_change(base, current)


def period_growth(
    series: Sequence[tuple[str, float | None]], unit: str | None
) -> list[PeriodGrowth]:
    """Growth of each period over the one before it (the first period has no predecessor)."""
    return [
        PeriodGrowth(period=period, growth=growth(prev, value, unit))
        for (_, prev), (period, value) in pairwise(series)
    ]


def cagr(
    start_value: float | None, end_value: float | None, start: date, end: date
) -> GrowthResult:
    if start_value is None or end_value is None:
        return _undefined("MISSING_VALUE", "pct", start_value, end_value)
    if start_value <= 0:
        return _undefined("NON_POSITIVE_START", "pct", start_value, end_value)
    if end_value < 0:
        return _undefined("NEGATIVE_END", "pct", start_value, end_value)
    years = (end - start).days / DAYS_PER_YEAR
    if years <= 0:
        return _undefined("ZERO_DURATION", "pct", start_value, end_value)
    rate = (end_value / start_value) ** (1 / years) - 1
    return GrowthResult(value=rate, unit="pct", base=start_value, current=end_value)
