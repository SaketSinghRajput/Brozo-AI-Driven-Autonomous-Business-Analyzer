"""Data facts the capability rules and plan templates read (TASK-ANALYSIS-002/005).

Deterministic summaries of Understanding + SemanticModel + ProfileResult; no data access.
The planning series uses the model's first time dimension (the understanding ranks the preferred
one first). History is counted at the *planning grain*: sub-monthly data is planned monthly
(business dashboards and forecasts are monthly), quarter/year data at its own grain. A date-part
year column (e.g. integer ``year``) spans its numeric min..max.
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.data.profiling.domain import ColumnProfile, ProfileResult, TimeGrain
from app.data.semantic.domain import SemanticDimension, SemanticEntity, SemanticModel
from app.data.understanding.domain import Understanding

PlanningGrain = Literal["month", "quarter", "year"]


@dataclass(frozen=True)
class TimeFacts:
    dimension: str  # semantic dimension name
    column: str
    grain: TimeGrain
    planning_grain: PlanningGrain
    start: date
    end: date
    periods: int  # calendar periods at the planning grain, first..last inclusive
    regularity: float


@dataclass(frozen=True)
class DataFacts:
    model: SemanticModel
    understanding: Understanding
    profile: ProfileResult
    time: TimeFacts | None
    target: str | None = None  # supervised target column chosen by the user, if any

    @property
    def dataset_version_id(self) -> str:
        return self.model.dataset_version_id

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.profile.columns if c.name == name), None)

    def entity(self, name: str) -> SemanticEntity | None:
        return next((e for e in self.model.entities if e.name == name), None)

    def categorical_dimensions(self, max_cardinality: float) -> list[SemanticDimension]:
        """Non-time dimensions with at most ``max_cardinality`` members, fewest members first."""
        found = []
        for d in self.model.dimensions:
            column = self.column(d.column)
            if d.type != "time" and column is not None and column.cardinality <= max_cardinality:
                found.append((column.cardinality, d.name, d))
        return [d for _, _, d in sorted(found, key=lambda t: (t[0], t[1]))]


def period_index(day: date, grain: PlanningGrain) -> int:
    if grain == "year":
        return day.year
    if grain == "quarter":
        return day.year * 4 + (day.month - 1) // 3
    return day.year * 12 + day.month - 1


def period_start(day: date, grain: PlanningGrain) -> date:
    if grain == "year":
        return date(day.year, 1, 1)
    if grain == "quarter":
        return date(day.year, (day.month - 1) // 3 * 3 + 1, 1)
    return date(day.year, day.month, 1)


def _range(column: ColumnProfile, grain: TimeGrain) -> tuple[date, date] | None:
    if column.temporal is not None:
        return column.temporal.min.date(), column.temporal.max.date()
    if grain == "year" and column.numeric is not None:
        first, last = int(column.numeric.min), int(column.numeric.max)
        if 1 <= first <= last <= 9999:
            return date(first, 1, 1), date(last, 12, 31)
    return None


def build_facts(
    understanding: Understanding,
    model: SemanticModel,
    profile: ProfileResult,
    target: str | None = None,
) -> DataFacts:
    facts = DataFacts(model, understanding, profile, None, target)
    regularity = {t.column: t.regularity for t in understanding.time_dimensions}
    for dim in model.dimensions:
        column = facts.column(dim.column)
        if dim.type != "time" or dim.grain is None or column is None:
            continue
        span = _range(column, dim.grain)
        if span is None:
            continue
        planning: PlanningGrain = dim.grain if dim.grain in ("quarter", "year") else "month"
        start, end = span
        periods = period_index(end, planning) - period_index(start, planning) + 1
        time = TimeFacts(
            dim.name,
            dim.column,
            dim.grain,
            planning,
            start,
            end,
            periods,
            regularity.get(dim.column, 0.0),
        )
        return DataFacts(model, understanding, profile, time, target)
    return facts
