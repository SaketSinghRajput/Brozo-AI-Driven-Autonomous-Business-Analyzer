"""Distinct values of a semantic dimension, for dashboard filter pickers (TASK-DASH-006).

The dimension is named by its semantic name (the same name ``DashboardFilter.dimension`` and
``Filter.dimension`` use) and resolved through the semantic model, never taken as a column name
from the request. The query runs on the locked DuckDB engine (no external access, timeout, row
cap) over the content-hash-checked stored version; values are sorted and bounded (<= 200).
Time dimensions are refused: their filter is a date range, not a pick list.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.analytics.source import DatasetSource
from app.analytics.sql.compiler import DATA_VIEW, quote
from app.analytics.sql.engine import AnalyticalEngine, QueryLimits
from app.analytics.sql.resolver import SemanticResolver
from app.analytics.sql.results import to_cell
from app.core.errors import AppValidationError, NotFoundError
from app.core.scope import WorkspaceScope
from app.data.domain import Cell
from app.data.repository import DatasetRepository
from app.data.understanding_store import UnderstandingStore
from app.infra.db import UnitOfWork
from app.infra.storage import Storage

MAX_VALUES = 200


class DimensionValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dimension: str
    values: list[Cell]
    truncated: bool  # more distinct values exist than ``limit``
    semantic_model_version: Annotated[int, Field(ge=1)]


class DimensionValuesService:
    def __init__(self, uow: UnitOfWork, storage: Storage) -> None:
        self._uow = uow
        self._source = DatasetSource(uow, storage)

    def values(
        self, scope: WorkspaceScope, dataset_id: str, version_id: str, name: str, limit: int
    ) -> DimensionValues:
        with self._uow(scope) as s:
            version = DatasetRepository(s).get_version(scope, version_id)
            model = UnderstandingStore(s).semantic_model(scope, version_id)
        if version is None or version.dataset_id != dataset_id or model is None:
            raise NotFoundError("dataset version not found or not mapped yet")
        dim = SemanticResolver(model).dimension(name)
        if dim.type == "time":
            raise AppValidationError("time dimensions are filtered by a date range")
        loaded = self._source.load(scope, version_id, model.version)
        column = quote(dim.column)
        sql = (
            f"SELECT DISTINCT {column} FROM {DATA_VIEW} "  # noqa: S608 (quoted model column)
            f"WHERE {column} IS NOT NULL ORDER BY 1"
        )
        engine = AnalyticalEngine(QueryLimits(timeout_s=5.0, max_rows=limit))
        table = loaded.table.select([dim.column])  # the engine sees only this column
        raw = engine.execute({DATA_VIEW: table}, sql, {}, limit)
        return DimensionValues(
            dimension=dim.name,
            values=[to_cell(row[0])[0] for row in raw.rows],
            truncated=raw.truncated,
            semantic_model_version=model.version,
        )
