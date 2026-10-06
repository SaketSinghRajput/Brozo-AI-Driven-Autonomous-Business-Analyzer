"""Tiny builders for ProfileResult / Understanding test inputs (no profiler needed)."""

from datetime import UTC, datetime
from typing import Any

from app.data.profiling.domain import (
    ColumnProfile,
    NumericStats,
    PhysicalType,
    ProfileResult,
    TemporalStats,
    TextStats,
    TimeGrain,
    TopK,
    TopKValue,
)


def numeric(lo: float, hi: float, *, neg: float = 0.0) -> NumericStats:
    mid = (lo + hi) / 2
    return NumericStats(
        min=lo, max=hi, mean=mid, median=mid, std=1.0, var=1.0, skew=0.0,
        quantiles={"q50": mid}, zero_ratio=0.0, neg_ratio=neg,
    )  # fmt: skip


def text(mixed: float = 0.0, mean_length: float = 8.0, **regex: float) -> TextStats:
    return TextStats(
        min_length=1, max_length=40, mean_length=mean_length,
        pattern_classes={"digits": 0.0, "alpha": 1.0 - mixed, "mixed": mixed},
        regex_hit_ratios=regex,
    )  # fmt: skip


def top(*values: str) -> TopK:
    n = len(values)
    return TopK(values=[TopKValue(value=v, count=1, frequency=1 / n) for v in values], masked=False)


def temporal(grain: TimeGrain | None = "day", regularity: float = 1.0) -> TemporalStats:
    return TemporalStats(
        min=datetime(2025, 1, 1, tzinfo=UTC), max=datetime(2025, 12, 31, tzinfo=UTC),
        granularity=grain, regularity=regularity, gaps=0, tz_mixed=False,
    )  # fmt: skip


def col(
    name: str,
    physical: PhysicalType = "string",
    *,
    unique: float = 0.1,
    card: int = 5,
    nulls: float = 0.0,
    **extra: Any,
) -> ColumnProfile:
    if physical in {"int", "float"} and "numeric" not in extra:
        extra["numeric"] = numeric(1, 100)
    if physical in {"date", "datetime"} and "temporal" not in extra:
        extra["temporal"] = temporal()
    if physical == "string" and "text" not in extra:
        extra["text"] = text()
    return ColumnProfile(
        name=name,
        physical_type=physical,
        parse_success_ratio=1.0,
        null_ratio=nulls,
        unique_ratio=unique,
        cardinality=card,
        **extra,
    )


def profile(*columns: ColumnProfile, rows: int = 1000, dsv: str = "dsv_test") -> ProfileResult:
    return ProfileResult(
        dataset_version_id=dsv,
        engine_version="test",
        row_count=rows,
        duplicate_rows=0,
        columns=list(columns),
        correlations=[],
        sampled=False,
        sample_size=None,
    )


def key(name: str, rows: int = 1000) -> ColumnProfile:
    return col(name, unique=1.0, card=rows, text=text(mixed=1.0))


def orders_profile(dsv: str = "dsv_orders") -> ProfileResult:
    return profile(
        key("order_id"),
        col("customer_id", unique=0.2, card=200, text=text(mixed=1.0)),
        col("product_id", unique=0.04, card=40, text=text(mixed=1.0)),
        col("order_date", "date", unique=0.3, card=300),
        col("quantity", "int", unique=0.01, card=10, numeric=numeric(1, 10)),
        col("unit_price", "float", unique=0.5, card=500, numeric=numeric(5, 500)),
        col("region", card=4),
        dsv=dsv,
    )
