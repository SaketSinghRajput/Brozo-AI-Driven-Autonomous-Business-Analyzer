"""AggregationService (TASK-ANALYSIS-008, design §22 + §62.1).

Re-aggregates a ``ResultTable`` to coarser groups (e.g. Region x Category → Region). The
additivity check runs before any SUM: non-additive values (ratios, prices, AOV) cannot be
summed, and semi-additive values (balances, stock) cannot be summed across the time column.
Null values are ignored; ``count`` counts non-null values. Invariant for additive sums: the sum
of the group totals equals the grand total.
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.analytics.domain import ResultTable
from app.core.errors import SchemaError
from app.data.domain import Cell
from app.data.understanding.domain import Additivity

Agg = Literal["sum", "avg", "min", "max", "count"]


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: list[Cell]
    value: float | None
    n: int  # non-null inputs


class AggregationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    by: list[str]
    agg: Agg
    unit: str | None
    groups: list[Group]
    total: float | None


def _apply(agg: Agg, xs: list[float]) -> float | None:
    if agg == "count":
        return float(len(xs))
    if not xs:
        return None
    if agg == "sum":
        return sum(xs)
    if agg == "avg":
        return sum(xs) / len(xs)
    return min(xs) if agg == "min" else max(xs)


def aggregate(
    table: ResultTable,
    value: str,
    agg: Agg,
    by: Sequence[str],
    additivity: Additivity,
) -> AggregationResult:
    names = [c.name for c in table.columns]
    if any(n not in names for n in [value, *by]):
        raise SchemaError("aggregation references columns that are not in the result")
    if agg == "sum" and additivity == "non_additive":
        raise SchemaError("a non-additive value cannot be summed")
    collapsed = [c for c in table.columns if c.name not in by and c.name != value]
    if (
        agg == "sum"
        and additivity == "semi_additive"
        and any(c.role == "period" for c in collapsed)
    ):
        raise SchemaError("a semi-additive value cannot be summed across time")
    v_index, keys = names.index(value), [names.index(b) for b in by]
    buckets: dict[tuple[Cell, ...], list[float]] = {}
    for row in table.rows:
        bucket = buckets.setdefault(tuple(row[i] for i in keys), [])
        cell = row[v_index]
        if isinstance(cell, int | float) and not isinstance(cell, bool):
            bucket.append(float(cell))
    ordered = sorted(buckets.items(), key=lambda kv: tuple((x is None, str(x)) for x in kv[0]))
    groups = [Group(key=list(k), value=_apply(agg, xs), n=len(xs)) for k, xs in ordered]
    everything = [x for xs in buckets.values() for x in xs]
    unit = "count" if agg == "count" else table.columns[v_index].unit
    return AggregationResult(
        by=list(by), agg=agg, unit=unit, groups=groups, total=_apply(agg, everything)
    )
