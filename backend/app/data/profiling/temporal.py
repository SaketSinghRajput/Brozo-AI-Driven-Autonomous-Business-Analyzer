"""Temporal profiling (TASK-PROFILE-005, TRD section 11.1).

granularity = mode of the successive differences of the sorted unique values, bucketed into
calendar grains (a month is 28-31 days, a quarter 89-92, a year 365-366); regularity = share of
differences in the mode bucket; gaps = differences longer than the mode grain's longest span.
A mode bucket that is no calendar grain gives granularity None (and no gap count).
"""

import datetime as dt
from collections import Counter

import polars as pl

from app.data.profiling.domain import TemporalStats, TimeGrain

_DAY = 86_400
# (grain, shortest span, longest span) in seconds, in ascending order.
GRAINS: tuple[tuple[TimeGrain, int, int], ...] = (
    ("second", 1, 1),
    ("minute", 60, 60),
    ("hour", 3_600, 3_600),
    ("day", _DAY, _DAY),
    ("week", 7 * _DAY, 7 * _DAY),
    ("month", 28 * _DAY, 31 * _DAY),
    ("quarter", 89 * _DAY, 92 * _DAY),
    ("year", 365 * _DAY, 366 * _DAY),
)
_ORDER: dict[TimeGrain | None, int] = {g: i for i, (g, _, _) in enumerate(GRAINS)}


def grain_of(seconds: float) -> TimeGrain | None:
    for grain, low, high in GRAINS:
        if low <= seconds <= high:
            return grain
    return None


def _aware(value: dt.date | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)


def temporal_stats(series: pl.Series, *, tz_mixed: bool = False) -> TemporalStats | None:
    unique = series.drop_nulls().unique().sort()
    if unique.len() == 0:
        return None
    seconds = [d.total_seconds() for d in unique.diff().drop_nulls().to_list()]
    counts = Counter(grain_of(s) for s in seconds)
    # Ties go to the finer grain so the result is deterministic.
    mode = min(counts, key=lambda g: (-counts[g], _ORDER.get(g, 99))) if counts else None
    longest = next((high for g, _, high in GRAINS if g == mode), None)
    return TemporalStats(
        min=_aware(unique[0]),
        max=_aware(unique[-1]),
        granularity=mode,
        regularity=counts[mode] / len(seconds) if seconds and mode else 0.0,
        gaps=sum(1 for s in seconds if s > longest) if longest else 0,
        tz_mixed=tz_mixed,
    )
