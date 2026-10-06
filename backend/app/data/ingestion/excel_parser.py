"""ExcelParser (TASK-DATA-004): .xlsx values only, sheet listing and selection.

python-calamine (Rust `calamine`, MIT) reads the cached value Excel stored for each formula
cell and never evaluates formulas, runs macros, or uses Python's XML parser. Macro-enabled
packages and zip bombs are rejected by FileInspector before this parser is chosen.
One dataset per selected sheet (TRD section 10); default is the first sheet.
"""

import datetime as dt
from collections.abc import Iterator

from python_calamine import CalamineWorkbook

from app.core.errors import DataError
from app.data.domain import Cell, FileFacts, ParsedBatch, ParseLimits
from app.data.ingestion.errors import reject


class ExcelParser:
    name = "xlsx"
    priority = 20

    def __init__(self, sheet: str | None = None, batch_rows: int = 50_000) -> None:
        self._sheet = sheet
        self._batch_rows = batch_rows
        self.flags: list[str] = []
        self.notes: dict[str, str] = {}
        self.sheets: list[str] = []

    def can_parse(self, facts: FileFacts) -> bool:
        return facts.detected_type == "xlsx"

    def iter_batches(self, facts: FileFacts, limits: ParseLimits) -> Iterator[ParsedBatch]:
        workbook = CalamineWorkbook.from_path(str(facts.path))
        self.sheets = list(workbook.sheet_names)
        if not self.sheets:
            raise reject(DataError, "EMPTY_FILE", "The workbook has no sheets.")
        name = self._sheet or self.sheets[0]
        if name not in self.sheets:
            raise reject(
                DataError,
                "SHEET_NOT_FOUND",
                "The selected sheet does not exist.",
                sheets=self.sheets,
            )
        self.notes = {"sheet": name}
        if len(self.sheets) > 1:
            self.flags.append("MULTIPLE_SHEETS")
        if (facts.container or {}).get("formula_sheets"):
            self.flags.append("FORMULA_CELLS_PRESENT")
        # ponytail: calamine materializes one sheet's cell range; bounded by the inspector's
        # uncompressed-size cap. Switch to a row-streaming reader if sheets outgrow it.
        rows = iter(workbook.get_sheet_by_name(name).iter_rows())
        header = next((r for r in rows if any(c != "" for c in r)), None)
        if header is None:
            raise reject(DataError, "EMPTY_FILE", "The selected sheet has no rows.")
        columns = ["" if c == "" else str(c) for c in header]
        batch: list[list[Cell]] = []
        seen = 0
        for raw in rows:
            cells = [_cell(c) for c in raw]
            if all(c is None for c in cells):
                continue
            seen += 1
            if seen > limits.max_rows:
                raise reject(DataError, "TOO_MANY_ROWS", f"More than {limits.max_rows} rows.")
            batch.append(cells)
            if len(batch) == self._batch_rows:
                yield _batch(columns, batch)
                batch = []
        yield _batch(columns, batch)


def _cell(value: object) -> Cell:
    if value == "" or value is None:
        return None
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    return str(value)  # timedelta, Excel error values: kept as literal text


def _batch(columns: list[str], rows: list[list[Cell]]) -> ParsedBatch:
    return ParsedBatch.model_construct(
        columns=columns, rows=rows, rejected_rows=[], total_rows_seen=len(rows)
    )
