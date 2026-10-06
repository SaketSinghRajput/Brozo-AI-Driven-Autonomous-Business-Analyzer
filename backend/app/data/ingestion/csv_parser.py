"""CsvParser (TASK-DATA-003): encoding from FileInspector, delimiter sniffing, header detection,
streaming batches, and a quarantine sample of malformed rows (reasons only, never row content).

Streams with the stdlib `csv` reader so memory is one batch, whatever the file size, and every
row's field count is visible (a malformed row is quarantined, not silently padded or dropped).
Quoting follows RFC 4180 (`"` with doubled-quote escapes); only the delimiter is sniffed.
"""

import csv
import io
import re
from collections import Counter
from collections.abc import Iterator

from app.core.errors import DataError
from app.data.domain import Cell, FileFacts, ParsedBatch, ParseLimits, RejectedRow
from app.data.ingestion.errors import reject

DELIMITERS = (",", ";", "\t", "|")
_SNIFF_CHARS = 64 * 1024
_SNIFF_ROWS = 50
_NUMBER_OR_DATE = re.compile(r"^[+-]?[\d.,/:\s-]+%?$")


def sniff_delimiter(sample: str) -> str:
    """Pick the delimiter giving the most consistent field count (>1) over the first rows."""
    lines = "\n".join(sample.splitlines()[:_SNIFF_ROWS])
    best, best_score = ",", (False, 0.0, 0)
    for delimiter in DELIMITERS:
        widths = [len(r) for r in csv.reader(io.StringIO(lines), delimiter=delimiter) if r]
        if not widths:
            continue
        mode, hits = Counter(widths).most_common(1)[0]
        score = (mode > 1, hits / len(widths), mode)
        if score > best_score:
            best, best_score = delimiter, score
    return best


def looks_like_header(row: list[str]) -> bool:
    cells = [c.strip() for c in row]
    # Duplicate names are allowed here: the Normalizer dedupes them and flags it.
    return all(cells) and not any(_NUMBER_OR_DATE.match(c) for c in cells)


class CsvParser:
    name = "csv"
    priority = 10

    def __init__(self, batch_rows: int = 50_000, reject_sample: int = 20) -> None:
        self._batch_rows = batch_rows
        self._reject_sample = reject_sample
        self.flags: list[str] = []
        self.notes: dict[str, str] = {}

    def can_parse(self, facts: FileFacts) -> bool:
        return facts.detected_type == "csv" and facts.encoding is not None

    def iter_batches(self, facts: FileFacts, limits: ParseLimits) -> Iterator[ParsedBatch]:
        encoding = facts.encoding or "utf-8"
        with facts.path.open(encoding=encoding, errors="replace", newline="") as f:
            delimiter = sniff_delimiter(f.read(_SNIFF_CHARS))
            f.seek(0)
            self.notes = {"delimiter": delimiter, "encoding": encoding}
            if delimiter != ",":
                self.flags.append("DELIMITER_NOT_COMMA")
            reader = csv.reader(f, delimiter=delimiter, quotechar='"', strict=False)
            try:
                yield from self._batches(reader, limits)
            except csv.Error:
                raise reject(
                    DataError,
                    "MALFORMED_CSV",
                    "The file could not be read as CSV.",
                    line=reader.line_num,
                    hint="Save the sheet as UTF-8 CSV or upload the .xlsx file directly.",
                ) from None

    def _batches(self, reader: Iterator[list[str]], limits: ParseLimits) -> Iterator[ParsedBatch]:
        first = next((row for row in reader if row), None)
        if first is None:
            raise reject(DataError, "EMPTY_FILE", "The file has no rows.")
        if looks_like_header(first):
            columns, pending = first, []
            self.notes["header"] = "detected"
        else:
            columns, pending = [f"column_{i + 1}" for i in range(len(first))], [first]
            self.flags.append("NO_HEADER_ROW")
            self.notes["header"] = "generated"
        width = len(columns)
        rows: list[list[Cell]] = []
        rejected: list[RejectedRow] = []
        total = in_batch = sampled = 0
        for row in _chain(pending, reader):
            if not row:
                continue
            total += 1
            in_batch += 1
            if total > limits.max_rows:
                raise reject(DataError, "TOO_MANY_ROWS", f"More than {limits.max_rows} rows.")
            reason = _row_problem(row, width, limits.max_field_chars)
            if reason is None:
                rows.append(list(row))
            elif sampled < self._reject_sample:
                sampled += 1
                rejected.append(RejectedRow(row_number=total, reason=reason))
            if in_batch == self._batch_rows:
                yield _batch(columns, rows, rejected, in_batch)
                rows, rejected, in_batch = [], [], 0
        yield _batch(columns, rows, rejected, in_batch)


def _row_problem(row: list[str], width: int, max_chars: int) -> str | None:
    if len(row) != width:
        return f"expected {width} fields, found {len(row)}"
    if any(len(cell) > max_chars for cell in row):
        return f"a field exceeds {max_chars} characters"
    return None


def _batch(
    columns: list[str], rows: list[list[Cell]], rejected: list[RejectedRow], seen: int
) -> ParsedBatch:
    # Shape is guaranteed by construction; skip per-cell validation of 50k-row batches.
    return ParsedBatch.model_construct(
        columns=columns, rows=rows, rejected_rows=rejected, total_rows_seen=seen
    )


def _chain(first: list[list[str]], rest: Iterator[list[str]]) -> Iterator[list[str]]:
    yield from first
    yield from rest
