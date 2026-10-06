"""Calendar arithmetic for ``compare_to`` and period series (design §21, TRD §15.2).

``previous_period``: when the range covers whole calendar months (start is the 1st, end is a
month end) it shifts by the same number of months, so Aug 1-31 compares with Jul 1-31 and
Jul 1-31 with Jun 1-30. Otherwise it is the equally long run of days right before ``start``.
``same_period_last_year``: both ends shift back one year; Feb 29 becomes Feb 28 and a month-end
stays a month-end.
"""

import calendar
from datetime import date, timedelta
from typing import Literal

CompareTo = Literal["previous_period", "same_period_last_year"]
Grain = Literal["day", "week", "month", "quarter", "year"]
_GRAIN_MONTHS = {"month": 1, "quarter": 3, "year": 12}


def add_months(day: date, months: int) -> date:
    year, month0 = divmod(day.year * 12 + day.month - 1 + months, 12)
    last = calendar.monthrange(year, month0 + 1)[1]
    return date(year, month0 + 1, min(day.day, last))


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def comparison_range(start: date, end: date, compare_to: CompareTo) -> tuple[date, date]:
    if start > end:
        raise ValueError("start must not be after end")
    if compare_to == "same_period_last_year":
        shifted_end = add_months(end, -12)
        if end == month_end(end):
            shifted_end = month_end(shifted_end)
        return add_months(start, -12), shifted_end
    if start.day == 1 and end == month_end(end):
        months = (end.year * 12 + end.month) - (start.year * 12 + start.month) + 1
        return add_months(start, -months), start - timedelta(days=1)
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def period_start(day: date, grain: Grain) -> date:
    if grain == "day":
        return day
    if grain == "week":  # ISO week, Monday start (matches DuckDB date_trunc('week'))
        return day - timedelta(days=day.weekday())
    months = _GRAIN_MONTHS[grain]
    return date(day.year, (day.month - 1) // months * months + 1, 1)


def next_period(start: date, grain: Grain) -> date:
    if grain == "day":
        return start + timedelta(days=1)
    if grain == "week":
        return start + timedelta(days=7)
    return add_months(start, _GRAIN_MONTHS[grain])


def calendar_periods(first: date, last: date, grain: Grain) -> list[date]:
    """Every period start from the period containing ``first`` to the one containing ``last``."""
    out, current = [], period_start(first, grain)
    while current <= last:
        out.append(current)
        current = next_period(current, grain)
    return out
