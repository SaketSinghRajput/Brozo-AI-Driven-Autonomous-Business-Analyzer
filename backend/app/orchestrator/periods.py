"""Period values an LLM puts into a time-dimension filter -> a date range (F1, BENCH-002).

Accepted values: a year (2025, "2025", 2025.0), a month ("2025-08"), a day ("2025-08-14" or a
date). ``eq`` covers that whole period with its grain; ``between`` spans first..last (grain: year
for two years, month for two months, else left to the intent); ``gte``/``lte`` give one open
end. Anything else returns None and the filter is kept as it is.
"""

import calendar
import re
from datetime import date
from typing import Literal

from app.planner.domain import Filter, FilterScalar

Grain = Literal["day", "month", "year"]
Period = tuple[date | None, date | None, Grain | None]
_YEAR = re.compile(r"^\d{4}$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def _span(value: FilterScalar) -> tuple[date, date, Grain] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, date):
        return value, value, "day"
    if isinstance(value, int | float):
        if not (float(value).is_integer() and 1 <= value <= 9999):
            return None
        year = int(value)
        return date(year, 1, 1), date(year, 12, 31), "year"
    text = value.strip()
    if _YEAR.match(text):
        return _span(int(text))
    if m := _MONTH.match(text):
        year, month = int(m[1]), int(m[2])
        if not 1 <= month <= 12:
            return None
        last = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last), "month"
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    return day, day, "day"


def lift(flt: Filter) -> Period | None:
    """(start, end, grain) of a filter on a time dimension; None when it is not a period."""
    value = flt.value
    if flt.op == "between" and isinstance(value, list):
        first, last = _span(value[0]), _span(value[1])
        if first is None or last is None or first[0] > last[1]:
            return None
        grain = first[2] if first[2] == last[2] and first[2] != "day" else None
        return first[0], last[1], grain
    if isinstance(value, list):
        return None
    span = _span(value)
    if span is None:
        return None
    if flt.op == "eq":
        return span
    if flt.op == "gte":
        return span[0], None, None
    if flt.op == "lte":
        return None, span[1], None
    return None
