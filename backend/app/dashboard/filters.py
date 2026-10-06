"""FilterBinder (TASK-DASH-006, design §28): dashboard filters → re-queries with new evidence.

``filters(facts)`` offers a date range on the planning time dimension and multi-selects on the
lowest-cardinality categorical dimensions. ``bind(query, filters, active)`` turns the active
filter values into ``Filter`` clauses on a widget's ``query`` (its ``MetricRequest``): multi
select → ``in``, single select → ``eq``, date range → ``between``. A clause replaces any filter
the widget already had on the same dimension. The frontend posts the bound request to
``POST /api/v1/analytics/query``, which runs it through the ToolExecutor as
``analytics.metric_query`` and returns the new evidence id; widgets without a ``query`` are not
filterable. The binding rule is this function; a client that re-implements it must match it.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import date

from app.analytics.domain import MetricRequest
from app.core.errors import AppValidationError
from app.dashboard.domain import DashboardFilter
from app.planner.domain import Filter, FilterScalar
from app.planner.facts import DataFacts

MAX_FILTERS = 3
MAX_MEMBERS = 50  # a multi-select over more members is not a usable filter
FilterValue = FilterScalar | Sequence[FilterScalar]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "dim"


class FilterBinder:
    def filters(self, facts: DataFacts) -> list[DashboardFilter]:
        found: list[DashboardFilter] = []
        if facts.time is not None:
            found.append(
                DashboardFilter(id="f_time", dimension=facts.time.dimension, type="date_range")
            )
        for dim in facts.categorical_dimensions(MAX_MEMBERS)[:MAX_FILTERS]:
            fid = f"f_{_slug(dim.name)}"
            found.append(DashboardFilter(id=fid, dimension=dim.name, type="multi_select"))
        return found

    def bind(
        self,
        query: MetricRequest,
        filters: Sequence[DashboardFilter],
        active: Mapping[str, FilterValue],
    ) -> MetricRequest:
        by_id = {f.id: f for f in filters}
        unknown = sorted(set(active) - set(by_id))
        if unknown:
            raise AppValidationError(f"unknown dashboard filters: {unknown}")
        clauses = [
            clause
            for fid, value in sorted(active.items())
            if (clause := self._clause(by_id[fid], value)) is not None
        ]
        bound = {c.dimension for c in clauses}
        kept = [f for f in query.filters if f.dimension not in bound]
        return MetricRequest.model_validate({**query.model_dump(), "filters": [*kept, *clauses]})

    @staticmethod
    def _clause(spec: DashboardFilter, value: FilterValue) -> Filter | None:
        values = list(value) if isinstance(value, list | tuple) else [value]
        if not values:
            return None  # an empty selection means "no filter"
        if spec.type == "date_range":
            if len(values) != 2 or not all(isinstance(v, date | str) for v in values):
                raise AppValidationError(f"{spec.id} needs [start, end]")
            return Filter(dimension=spec.dimension, op="between", value=values)
        if spec.type == "single_select":
            return Filter(dimension=spec.dimension, op="eq", value=values[0])
        return Filter(dimension=spec.dimension, op="in", value=values)
