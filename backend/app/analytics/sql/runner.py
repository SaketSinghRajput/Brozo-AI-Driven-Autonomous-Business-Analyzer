"""MetricRequest → QueryCompiler → AnalyticalEngine → ResultValidator/Serializer (design §21)."""

from dataclasses import dataclass

import pyarrow as pa
from pydantic import JsonValue

from app.analytics.domain import MetricRequest, ResultTable
from app.analytics.sql.compiler import DATA_VIEW, CompiledQuery, QueryCompiler
from app.analytics.sql.engine import ENGINE_VERSION, AnalyticalEngine
from app.analytics.sql.results import to_result_table
from app.data.semantic.domain import SemanticModel


def year_columns(model: SemanticModel, data: pa.Table) -> frozenset[str]:
    """Time dimensions stored as integers (a year column such as ``crop_year = 2024``)."""
    names = set(data.column_names)
    return frozenset(
        d.column
        for d in model.dimensions
        if d.type == "time"
        and d.column in names
        and pa.types.is_integer(data.schema.field(d.column).type)
    )


@dataclass(frozen=True, slots=True)
class QueryRun:
    table: ResultTable
    compiled: CompiledQuery

    def record(self) -> dict[str, JsonValue]:
        """The query as evidence stores it (``QueryInfo`` shape: engine, sql, params)."""
        params: dict[str, JsonValue] = dict(self.compiled.json_params())
        return {"engine": ENGINE_VERSION, "sql": self.compiled.sql, "params": params}


class MetricQueryRunner:
    def __init__(self, engine: AnalyticalEngine, model: SemanticModel, data: pa.Table) -> None:
        self._engine = engine
        self._compiler = QueryCompiler(model, year_columns=year_columns(model, data))
        self._views = {DATA_VIEW: data}
        self.model = model

    def run(self, request: MetricRequest) -> QueryRun:
        compiled = self._compiler.compile(request)
        raw = self._engine.execute(self._views, compiled.sql, compiled.params, compiled.limit)
        return QueryRun(to_result_table(raw, compiled), compiled)
