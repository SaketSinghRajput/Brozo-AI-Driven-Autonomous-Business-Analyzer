"""Storage-level ingestion pipeline (design.md section 10), independent of the database.

parse (factory) → normalize (per batch) → staging Parquet → infer types → typed Parquet in the
store. `IngestionService` wraps this with the version state machine; tests call it directly.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

import polars as pl
from pydantic import BaseModel, ConfigDict

from app.core.errors import DataError
from app.data.domain import FileFacts, ParseLimits, RejectedRow
from app.data.ingestion.csv_parser import CsvParser
from app.data.ingestion.errors import reject
from app.data.ingestion.excel_parser import ExcelParser
from app.data.ingestion.factory import ParserFactory, ReportingParser
from app.data.ingestion.inference import ColumnDecision, infer_columns
from app.data.ingestion.normalizer import NormalizationRules, Normalizer, normalize_headers
from app.data.ingestion.writer import WRITER, StagingWriter, staging_key, write_final
from app.infra.storage import Storage

_EXPECTED_SUFFIX = {".csv": "csv", ".tsv": "csv", ".txt": "csv", ".xlsx": "xlsx"}


class IngestReport(BaseModel):
    """Persisted with the version (`dataset_versions.ingest_report`); never holds row content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_filename: str | None
    detected_type: str
    encoding: str | None
    parser: str
    notes: dict[str, str]
    sheets: list[str]
    rows_seen: int
    rows_ingested: int
    rows_rejected: int
    rejected_sample: list[RejectedRow]
    columns: list[ColumnDecision]
    flags: list[str]
    normalization_rules_version: str
    writer: str


@dataclass(frozen=True)
class PipelineLimits:
    parse: ParseLimits
    batch_rows: int = 50_000


def parser_for(facts: FileFacts, sheet: str | None, limits: PipelineLimits) -> ReportingParser:
    """Raises UNSUPPORTED_FORMAT; called during VALIDATING so the failure is a validation one."""
    return ParserFactory(
        [CsvParser(limits.batch_rows), ExcelParser(sheet, limits.batch_rows)]
    ).for_(facts)


def ingest_file(
    facts: FileFacts,
    parser: ReportingParser,
    *,
    filename: str | None,
    limits: PipelineLimits,
    storage: Storage,
    key: str,
    workdir: Path,
    on_batch: Callable[[int], None] = lambda _: None,
) -> IngestReport:
    rules = NormalizationRules()
    staging_path = workdir / "staging.parquet"
    normalizer: Normalizer | None = None
    staging: StagingWriter | None = None
    header_flags: list[str] = []
    seen = kept = 0
    sample: list[RejectedRow] = []
    for batch in parser.iter_batches(facts, limits.parse):
        if normalizer is None or staging is None:
            columns, header_flags = normalize_headers(batch.columns)
            normalizer = Normalizer(columns, rules)
            staging = StagingWriter(staging_path, len(columns))
        staging.write(normalizer.normalize(batch))
        seen += batch.total_rows_seen
        kept += len(batch.rows)
        sample.extend(batch.rejected_rows)
        on_batch(seen)
    if normalizer is None or staging is None:
        raise reject(DataError, "EMPTY_FILE", "The file has no rows.")
    staging.close()
    if seen == 0:
        raise reject(DataError, "NO_DATA_ROWS", "The file has a header but no data rows.")
    if (seen - kept) / seen > limits.parse.max_reject_ratio:
        raise reject(
            DataError,
            "TOO_MANY_BAD_ROWS",
            f"{seen - kept} of {seen} rows are malformed.",
            hint="Check the delimiter and quoting, then upload again.",
        )
    names = [(staging_key(i), c.name, c.source_name) for i, c in enumerate(normalizer.columns)]
    decisions = infer_columns(pl.scan_parquet(staging_path), names)
    with storage.writer(key) as sink:
        stored = write_final(staging_path, decisions, sink, limits.batch_rows)
    flags = [*parser.flags, *header_flags, *normalizer.flags()]
    flags += [f for d in decisions for f in d.flags] + _file_flags(facts, filename, seen - kept)
    return IngestReport(
        source_filename=filename,
        detected_type=facts.detected_type,
        encoding=facts.encoding,
        parser=parser.name,
        notes=parser.notes,
        sheets=getattr(parser, "sheets", []),
        rows_seen=seen,
        rows_ingested=stored,
        rows_rejected=seen - kept,
        rejected_sample=sample,
        columns=decisions,
        flags=sorted(set(flags)),
        normalization_rules_version=rules.version,
        writer=WRITER,
    )


def _file_flags(facts: FileFacts, filename: str | None, rejected: int) -> list[str]:
    flags = []
    if facts.encoding and not facts.encoding.lower().replace("_", "-").startswith("utf"):
        flags.append("ENCODING_NON_UTF8")
    suffix = PurePath(filename).suffix.lower() if filename else ""
    if suffix and _EXPECTED_SUFFIX.get(suffix) != facts.detected_type:
        flags.append("EXTENSION_MISMATCH")
    if rejected:
        flags.append("MALFORMED_ROW_FIELD_COUNT")
    return flags
