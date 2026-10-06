"""Analytics contracts: MetricRequest (design §21) and the typed ResultTable (TASK-SQL-006).

``MetricRequest`` adds ``time_dimension`` to the design §21 sketch: a semantic model can hold
several time dimensions and ``TimeSpec`` (planner contract) does not name one. When omitted and
the model has exactly one time dimension, that one is used.

``compare_to`` semantics: the request's ``time`` range is the current period; the comparison
range is derived by ``app.analytics.sql.periods``. With ``compare_to`` the result holds range
totals (grouped by ``dimensions`` only), not a time series.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data.domain import Cell, Sha256
from app.planner.domain import Filter, TimeSpec

SemanticName = Annotated[str, Field(min_length=1, max_length=200)]
CompareTo = Literal["previous_period", "same_period_last_year"]
ColumnType = Literal["string", "integer", "float", "boolean", "date", "timestamp", "null"]
ColumnRole = Literal["dimension", "period", "value", "compare_value"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricRequest(Contract):
    metric: SemanticName
    dimensions: Annotated[list[SemanticName], Field(max_length=5)] = []
    filters: Annotated[list[Filter], Field(max_length=20)] = []
    time: TimeSpec | None = None
    time_dimension: SemanticName | None = None
    compare_to: CompareTo | None = None
    limit: Annotated[int, Field(ge=1, le=10_000)] = 1000

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.compare_to and not (self.time and self.time.start and self.time.end):
            raise ValueError("compare_to needs time.start and time.end")
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("dimensions must be unique")
        return self


class ResultColumn(Contract):
    name: str
    type: ColumnType
    role: ColumnRole
    unit: str | None = None  # "currency:INR", "pct", "count"; None = unitless


class ResultTable(Contract):
    schema_version: Literal["1.0"] = "1.0"
    columns: list[ResultColumn]
    rows: list[list[Cell]]
    row_count: Annotated[int, Field(ge=0)]
    truncated: bool  # the row cap was hit; rows beyond it were not returned
    warnings: list[str] = []
    result_hash: Sha256  # over columns + rows only, so it is stable for the same result

    def column(self, name: str) -> list[Cell]:
        index = [c.name for c in self.columns].index(name)
        return [row[index] for row in self.rows]

    def first_of_role(self, role: ColumnRole) -> ResultColumn | None:
        return next((c for c in self.columns if c.role == role), None)
