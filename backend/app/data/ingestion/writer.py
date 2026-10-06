"""ColumnarWriter + ContentHasher (TASK-DATA-008, ADR-0004, ADR-0023, RES-014 R4).

Two passes, both one batch at a time so memory stays bounded:
1. `StagingWriter` streams normalized string cells to a local, all-string Parquet file
   (columns keyed `c0..cN`, so header text never reaches an expression or regex).
2. `write_final` re-reads it by batch, applies the inferred conversions and streams the typed
   Parquet into `Storage.writer`, which does temp → fsync → atomic rename (partial writes are
   never visible). Any conversion that would turn a value into NULL aborts: no silent loss.
Writer options are pinned (pyarrow, zstd, statistics, fixed batch size) so bytes are
deterministic for a pinned pyarrow version; `content_hash` is SHA-256 of the stored bytes.
"""

import hashlib
from pathlib import Path
from typing import BinaryIO

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from app.core.errors import DataError
from app.data.ingestion.errors import reject
from app.data.ingestion.inference import ColumnDecision, convert_expr
from app.infra.storage import Storage

WRITER = f"pyarrow-{pa.__version__}"


def staging_key(index: int) -> str:
    return f"c{index}"


class StagingWriter:
    def __init__(self, path: Path, width: int) -> None:
        self._schema = pa.schema([(staging_key(i), pa.string()) for i in range(width)])
        self._writer = pq.ParquetWriter(path, self._schema, compression="zstd")

    def write(self, rows: list[list[str | None]]) -> None:
        if rows:
            columns = [pa.array(col, pa.string()) for col in zip(*rows, strict=True)]
            self._writer.write_table(pa.Table.from_arrays(columns, schema=self._schema))

    def close(self) -> None:
        self._writer.close()


def write_final(
    staging: Path, decisions: list[ColumnDecision], sink: BinaryIO, batch_rows: int
) -> int:
    """Convert and stream the staging file into `sink`; returns the stored row count."""
    schema = pa.schema([(d.name, d.arrow_type()) for d in decisions])
    exprs = [convert_expr(staging_key(i), d) for i, d in enumerate(decisions)]
    rows = 0
    with pq.ParquetWriter(sink, schema, compression="zstd", write_statistics=True) as writer:
        for batch in pq.ParquetFile(staging).iter_batches(batch_size=batch_rows):
            raw = pl.DataFrame(pl.from_arrow(batch))
            out = raw.select(exprs)
            for i, d in enumerate(decisions):
                if out[d.name].null_count() != raw[staging_key(i)].null_count():
                    raise reject(
                        DataError,
                        "TYPE_CONVERSION_FAILED",
                        f"Column {d.name!r} could not be converted without losing values.",
                    )
            table = out.to_arrow(compat_level=pl.CompatLevel.oldest()).cast(schema)
            writer.write_table(table, row_group_size=batch_rows)
            rows += out.height
    return rows


def content_hash(storage: Storage, key: str) -> str:
    """ContentHasher: SHA-256 of the stored bytes, streamed (`sha256:<hex>`, ADR-0023)."""
    digest = hashlib.sha256()
    with storage.open_read(key) as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
