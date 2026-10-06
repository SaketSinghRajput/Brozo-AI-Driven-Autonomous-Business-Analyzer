"""TimeSeriesService (TASK-ANALYSIS-013, design §22).

- ``resample``: points → calendar periods (sum / mean / last per period).
- ``fill_calendar``: every period in [start, end]; missing periods are always *reported*. The fill
  policy is explicit: ``none`` (leave null, the default), ``zero`` (only valid for additive
  flows where "no rows" really means 0), ``forward`` (carry the last value).
- ``trend_test``: Mann-Kendall test with tie-corrected variance, normal approximation with
  continuity correction, two-sided p-value; Sen's slope per period. Assumptions are listed in the
  result. Needs at least 4 non-null values.
"""

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.analytics.sql.periods import Grain, calendar_periods, period_start

FillPolicy = Literal["none", "zero", "forward"]
MK_ASSUMPTIONS = [
    "observations are serially independent (autocorrelation inflates significance)",
    "the trend is monotonic, not necessarily linear",
    "normal approximation of S; approximate for n < 10",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeriesPoint(Contract):
    period: date
    value: float | None
    filled: bool = False


class Series(Contract):
    grain: Grain
    fill_policy: FillPolicy
    points: list[SeriesPoint]
    missing_periods: list[date]


class TrendTest(Contract):
    test: Literal["mann_kendall"] = "mann_kendall"
    n: int
    s: int | None = None
    tau: float | None = None
    z: float | None = None
    p_value: float | None = None
    sen_slope: float | None = None  # value change per period
    alpha: float
    direction: Literal["increasing", "decreasing", "no_trend"] | None = None
    assumptions: list[str] = MK_ASSUMPTIONS
    undefined: Literal["TOO_FEW_POINTS", "ALL_TIED"] | None = None


def resample(
    points: Sequence[tuple[date, float | None]],
    grain: Grain,
    agg: Literal["sum", "mean", "last"] = "sum",
) -> list[tuple[date, float | None]]:
    buckets: dict[date, list[float]] = {}
    for day, value in sorted(points, key=lambda p: p[0]):
        bucket = buckets.setdefault(period_start(day, grain), [])
        if value is not None:
            bucket.append(float(value))
    out: list[tuple[date, float | None]] = []
    for period, xs in sorted(buckets.items()):
        if not xs:
            out.append((period, None))
        elif agg == "sum":
            out.append((period, sum(xs)))
        elif agg == "mean":
            out.append((period, statistics.fmean(xs)))
        else:
            out.append((period, xs[-1]))
    return out


def fill_calendar(
    points: Sequence[tuple[date, float | None]],
    grain: Grain,
    start: date,
    end: date,
    fill_policy: FillPolicy = "none",
) -> Series:
    known = {period_start(d, grain): v for d, v in points}
    series: list[SeriesPoint] = []
    missing: list[date] = []
    last: float | None = None
    for period in calendar_periods(start, end, grain):
        if period in known:
            value = known[period]
            series.append(SeriesPoint(period=period, value=value))
            last = value if value is not None else last
            continue
        missing.append(period)
        fill = {"none": None, "zero": 0.0, "forward": last}[fill_policy]
        series.append(SeriesPoint(period=period, value=fill, filled=fill is not None))
    return Series(grain=grain, fill_policy=fill_policy, points=series, missing_periods=missing)


def trend_test(values: Sequence[float | None], alpha: float = 0.05) -> TrendTest:
    indexed = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    n = len(indexed)
    if n < 4:
        return TrendTest(n=n, alpha=alpha, undefined="TOO_FEW_POINTS")
    s = 0
    slopes: list[float] = []
    for a in range(n):
        for b in range(a + 1, n):
            (ia, xa), (ib, xb) = indexed[a], indexed[b]
            s += (xb > xa) - (xb < xa)
            slopes.append((xb - xa) / (ib - ia))
    tie_term = sum(t * (t - 1) * (2 * t + 5) for t in Counter(x for _, x in indexed).values())
    variance = (n * (n - 1) * (2 * n + 5) - tie_term) / 18
    if variance == 0:
        return TrendTest(n=n, s=s, alpha=alpha, undefined="ALL_TIED")
    z = 0.0 if s == 0 else (s - math.copysign(1, s)) / math.sqrt(variance)
    p_value = math.erfc(abs(z) / math.sqrt(2))
    direction: Literal["increasing", "decreasing", "no_trend"] = "no_trend"
    if p_value < alpha:
        direction = "increasing" if s > 0 else "decreasing"
    return TrendTest(
        n=n,
        s=s,
        tau=s / (n * (n - 1) / 2),
        z=z,
        p_value=p_value,
        sen_slope=statistics.median(slopes),
        alpha=alpha,
        direction=direction,
    )
