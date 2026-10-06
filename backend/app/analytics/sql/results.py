"""ResultValidator + ResultSerializer (TASK-SQL-006, design §21).

Validation: the engine returned exactly the columns the compiler promised, in order; the row cap
is reported (``truncated``), never hidden; all-null aggregates and non-finite numbers are
reported as warnings. Serialization: Arrow types → contract column types, values → JSON scalars
(dates as ISO strings, decimals as int/float), then a SHA-256 over columns + rows.
"""

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal

import pyarrow as pa

from app.analytics.domain import ColumnType, ResultColumn, ResultTable
from app.analytics.sql.compiler import CompiledQuery
from app.analytics.sql.engine import RawResult
from app.core.errors import ToolError
from app.data.domain import Cell


def column_type(arrow_type: pa.DataType) -> ColumnType:
    if pa.types.is_null(arrow_type):
        return "null"
    if pa.types.is_boolean(arrow_type):
        return "boolean"
    if pa.types.is_integer(arrow_type):
        return "integer"
    if pa.types.is_floating(arrow_type) or pa.types.is_decimal(arrow_type):
        return "float"
    if pa.types.is_date(arrow_type):
        return "date"
    if pa.types.is_timestamp(arrow_type):
        return "timestamp"
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return "string"
    raise ToolError("query produced an unsupported column type")


def to_cell(value: object) -> tuple[Cell, bool]:
    """JSON-safe cell, plus whether a non-finite float had to be dropped."""
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None, True
    if isinstance(value, datetime | date):
        return value.isoformat(), False
    if value is None or isinstance(value, str | int | float | bool):
        return value, False
    raise ToolError("query produced an unsupported value")


def table_hash(columns: list[ResultColumn], rows: list[list[Cell]]) -> str:
    payload = {"columns": [c.model_dump() for c in columns], "rows": rows}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def to_result_table(raw: RawResult, compiled: CompiledQuery) -> ResultTable:
    expected = [c.name for c in compiled.columns]
    if raw.schema.names != expected:
        raise ToolError("query result does not match the compiled column list")
    columns = [
        ResultColumn(name=c.name, type=column_type(f.type), role=c.role, unit=c.unit)
        for c, f in zip(compiled.columns, raw.schema, strict=True)
    ]
    warnings = list(compiled.warnings)
    rows: list[list[Cell]] = []
    dropped = False
    for raw_row in raw.rows:
        row = []
        for value in raw_row:
            cell, was_dropped = to_cell(value)
            dropped |= was_dropped
            row.append(cell)
        rows.append(row)
    if dropped:
        warnings.append("non-finite values were replaced by null")
    value_index = expected.index("value")
    if rows and all(r[value_index] is None for r in rows):
        warnings.append("the aggregate is null for every row (no matching data)")
    if raw.truncated:
        warnings.append(f"result truncated at {len(rows)} rows")
    return ResultTable(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=raw.truncated,
        warnings=warnings,
        result_hash=table_hash(columns, rows),
    )
