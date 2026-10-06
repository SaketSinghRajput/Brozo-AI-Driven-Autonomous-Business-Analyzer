"""Bounded tabular preview of a tool result (dashboard contract gap (a); design §27-28).

Chart and table widgets need the rows behind an evidence record, not just ``result.value``.
Option chosen: the ``EvidenceRecorder`` stores a preview of the output's table (<= ``MAX_ROWS``
rows, typed columns) in ``evidence.result_rows``, written in the evidence INSERT, and
``GET /api/v1/evidence/{id}/data`` serves it under the same VIEW_DATASET + workspace scope as the
evidence itself. Why: the rows are the exact output the evidence hash covers (no re-run that
could drift after an engine upgrade), immutable like the evidence, cheap to serve, and the
Evidence contract is unchanged.

Which table: ``output.table`` (a ``ResultTable``) when present; otherwise the first list of
objects under a preferred key (``points``, ``items``, ``findings``, ``anomalies``, ``segments``),
else the first list of objects found breadth-first. Nested objects are flattened one level
(``means.mrr``); lists inside rows are dropped. Domain-agnostic: no tool name is special-cased.
"""

import re
from collections import deque
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_ROWS = 500
PREFERRED = ("points", "items", "findings", "anomalies", "segments")
Cell = str | int | float | bool | None
PreviewType = Literal["string", "integer", "float", "boolean", "date", "timestamp", "null"]
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreviewColumn(Contract):
    name: str
    type: PreviewType
    unit: str | None = None  # evidence unit vocabulary: "currency:INR", "pct" (fraction), ...


class RowsPreview(Contract):
    columns: list[PreviewColumn]
    rows: list[list[Cell]]
    truncated: bool  # the source had more than MAX_ROWS rows, or was itself truncated
    row_count: Annotated[int, Field(ge=0)]  # rows in the source list


class EvidenceData(Contract):
    """``GET /evidence/{id}/data``: one object per row, keyed by column name."""

    evidence_id: str
    columns: list[PreviewColumn]
    rows: list[dict[str, Cell]]
    truncated: bool
    row_count: Annotated[int, Field(ge=0)]


def _cell_type(value: Cell) -> PreviewType:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if _DATE.match(value):
        return "date"
    return "timestamp" if _TIMESTAMP.match(value) else "string"


def _column_type(values: list[Cell]) -> PreviewType:
    types = {_cell_type(v) for v in values} - {"null"}
    if types and types <= {"integer", "float"}:
        return "float" if "float" in types else "integer"
    if not types:
        return "null"
    return types.pop() if len(types) == 1 else "string"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _flat(row: dict[str, Any]) -> dict[str, Cell]:
    flat: dict[str, Cell] = {}
    for key, value in row.items():
        if _is_scalar(value):
            flat[key] = value
        elif isinstance(value, dict):
            flat |= {f"{key}.{k}": v for k, v in value.items() if _is_scalar(v)}
    return flat


def _object_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _find_list(output: dict[str, Any]) -> list[dict[str, Any]] | None:
    first: list[dict[str, Any]] | None = None
    preferred: dict[str, list[dict[str, Any]]] = {}
    queue: deque[dict[str, Any]] = deque([output])
    while queue:
        node = queue.popleft()
        for key, value in node.items():
            if _object_list(value):
                preferred.setdefault(key, value)
                first = first or value
            elif isinstance(value, dict):
                queue.append(value)
    return next((preferred[k] for k in PREFERRED if k in preferred), first)


def _from_table(table: dict[str, Any]) -> RowsPreview:
    rows = [list(r) for r in table.get("rows", [])]
    columns = [
        PreviewColumn(name=str(c["name"]), type=c.get("type", "string"), unit=c.get("unit"))
        for c in table["columns"]
    ]
    return RowsPreview(
        columns=columns,
        rows=rows[:MAX_ROWS],
        truncated=bool(table.get("truncated")) or len(rows) > MAX_ROWS,
        row_count=len(rows),
    )


def rows_preview(output: dict[str, Any]) -> RowsPreview | None:
    """The output's table, bounded to MAX_ROWS rows; None when the output has no table."""
    table = output.get("table")
    if isinstance(table, dict) and isinstance(table.get("columns"), list):
        return _from_table(table)
    found = _find_list(output)
    if found is None:
        return None
    flat = [_flat(r) for r in found]
    names = list(dict.fromkeys(k for r in flat for k in r))
    if not names:
        return None
    unit = output.get("unit") if isinstance(output.get("unit"), str) else None
    columns = [
        PreviewColumn(
            name=n,
            type=_column_type([r.get(n) for r in flat]),
            unit=unit if n == "value" else None,
        )
        for n in names
    ]
    return RowsPreview(
        columns=columns,
        rows=[[r.get(n) for n in names] for r in flat[:MAX_ROWS]],
        truncated=len(flat) > MAX_ROWS,
        row_count=len(flat),
    )


def as_data(evidence_id: str, preview: RowsPreview | None) -> EvidenceData:
    if preview is None:
        return EvidenceData(
            evidence_id=evidence_id, columns=[], rows=[], truncated=False, row_count=0
        )
    names = [c.name for c in preview.columns]
    return EvidenceData(
        evidence_id=evidence_id,
        columns=preview.columns,
        rows=[dict(zip(names, row, strict=True)) for row in preview.rows],
        truncated=preview.truncated,
        row_count=preview.row_count,
    )
