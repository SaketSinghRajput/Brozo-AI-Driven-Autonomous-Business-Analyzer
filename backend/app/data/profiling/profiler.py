"""StructuralProfiler + column assembly (TASK-PROFILE-002..008) → `ProfileResult`.

Sampling policy (design section 12): counts, nulls, cardinality, numeric stats, top-k, text and
temporal stats use every row; correlations use a seeded sample of at most `sample_rows` rows,
and then `sampled=true, sample_size` are recorded. Deterministic: same data → same profile.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl

from app.data.profiling.domain import (
    ColumnProfile,
    PhysicalType,
    ProfileResult,
    TopK,
    TopKValue,
    UnitHint,
)
from app.data.profiling.numeric import correlations, numeric_stats, outliers
from app.data.profiling.signals import is_pii_like, mask, text_stats
from app.data.profiling.temporal import temporal_stats

ENGINE_VERSION = "profile-1.0"
TOP_K = 10
MAX_CORRELATION_COLUMNS = 20
SAMPLE_SEED = 0


@dataclass(frozen=True)
class IngestColumn:
    """What ingestion decided for a column (from the version's ingest report)."""

    physical_type: PhysicalType
    parse_success_ratio: float
    tz_mixed: bool = False
    unit_hint: UnitHint | None = None


def _dtype_type(dtype: pl.DataType) -> PhysicalType:
    if dtype.is_integer():
        return "int"
    if dtype.is_float():
        return "float"
    if dtype == pl.Boolean:
        return "bool"
    if dtype == pl.Date:
        return "date"
    if isinstance(dtype, pl.Datetime):
        return "datetime"
    return "string"


def _top_k(series: pl.Series, non_null: int, masked: bool) -> TopK | None:
    if non_null == 0:
        return None
    values = series.drop_nulls().cast(pl.String).rename("value")
    counts = values.value_counts(name="count")
    top = counts.sort(["count", "value"], descending=[True, False]).head(TOP_K)
    return TopK(
        values=[
            TopKValue(value=mask(v) if masked else v, count=c, frequency=c / non_null)
            for v, c in top.iter_rows()
        ],
        masked=masked,
    )


def profile_column(series: pl.Series, row_count: int, meta: IngestColumn | None) -> ColumnProfile:
    nulls = series.null_count()
    non_null = row_count - nulls
    cardinality = series.drop_nulls().n_unique()
    physical = meta.physical_type if meta else _dtype_type(series.dtype)
    numeric = physical in ("int", "float")
    texty = physical in ("string", "mixed")
    text = text_stats(series) if texty else None
    temporal = None
    if physical in ("date", "datetime"):
        temporal = temporal_stats(series, tz_mixed=bool(meta and meta.tz_mixed))
    return ColumnProfile(
        name=series.name,
        physical_type=physical,
        parse_success_ratio=meta.parse_success_ratio if meta else 1.0,
        null_ratio=nulls / row_count if row_count else 0.0,
        unique_ratio=cardinality / non_null if non_null else 0.0,
        cardinality=cardinality,
        numeric=numeric_stats(series) if numeric else None,
        categorical=(
            _top_k(series, non_null, is_pii_like(series.name, text))
            if texty or physical == "bool"
            else None
        ),
        temporal=temporal,
        text=text,
        outliers=outliers(series) if numeric else None,
        unit_hint=meta.unit_hint if meta else None,
    )


def profile_frame(
    frame: pl.DataFrame,
    version_id: str,
    ingest: Mapping[str, IngestColumn],
    *,
    sample_rows: int = 100_000,
) -> ProfileResult:
    rows = frame.height
    columns = [profile_column(frame[name], rows, ingest.get(name)) for name in frame.columns]
    numeric = [c.name for c in columns if c.physical_type in ("int", "float")]
    sampled = rows > sample_rows
    sample = frame.sample(sample_rows, seed=SAMPLE_SEED) if sampled else frame
    return ProfileResult(
        dataset_version_id=version_id,
        engine_version=ENGINE_VERSION,
        row_count=rows,
        duplicate_rows=rows - frame.unique().height if frame.width else 0,
        columns=columns,
        correlations=correlations(sample, numeric[:MAX_CORRELATION_COLUMNS]),
        sampled=sampled,
        sample_size=sample_rows if sampled else None,
    )
