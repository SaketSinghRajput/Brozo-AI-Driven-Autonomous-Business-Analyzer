"""TASK-PROFILE-002..008, PROFILE-010: profiler math vs hand computation, edge-case invariants."""

import datetime as dt
import math

import polars as pl
import pytest

from app.data.profiling.domain import ProfileResult
from app.data.profiling.numeric import numeric_stats, outliers
from app.data.profiling.profiler import IngestColumn, profile_frame
from app.data.profiling.service import profile_flags
from app.data.profiling.signals import SemanticSignalExtractor, name_tokens
from app.data.profiling.temporal import temporal_stats

VALUES = [1.0, 2.0, 3.0, 4.0, 100.0]


def test_numeric_stats_match_hand_computation() -> None:
    stats = numeric_stats(pl.Series("x", [*VALUES, None]))
    assert stats is not None
    n, mean = 5, 22.0
    dev = [v - mean for v in VALUES]
    m2 = sum(d * d for d in dev) / n
    m3 = sum(d**3 for d in dev) / n
    skew = (m3 / m2**1.5) * math.sqrt(n * (n - 1)) / (n - 2)  # adjusted Fisher-Pearson
    assert (stats.min, stats.max, stats.mean, stats.median) == (1.0, 100.0, 22.0, 3.0)
    assert stats.var == pytest.approx(1902.5)  # 7610 / (n - 1)
    assert stats.std == pytest.approx(math.sqrt(1902.5))
    assert stats.skew == pytest.approx(skew)
    # numpy "linear": position (n - 1) * q.
    assert stats.quantiles["q25"] == 2.0
    assert stats.quantiles["q75"] == 4.0
    assert stats.quantiles["q01"] == pytest.approx(1.04)
    assert stats.quantiles["q99"] == pytest.approx(4 + 0.96 * 96)
    assert (stats.zero_ratio, stats.neg_ratio) == (0.0, 0.0)


def test_iqr_outlier_found_by_index_not_value() -> None:
    summary = outliers(pl.Series("x", [*VALUES, None]))
    assert summary is not None
    assert (summary.method, summary.count, summary.sample_indices) == ("iqr", 1, [4])


@pytest.mark.parametrize(
    ("values", "count"), [([7.0, 7.0, 7.0, 7.0, 99.0], 1), ([5.0, 5.0, 5.0], 0)]
)
def test_robust_fallback_when_iqr_is_zero(values: list[float], count: int) -> None:
    summary = outliers(pl.Series("x", values))
    assert summary is not None
    assert (summary.method, summary.count) == ("mad", count)


def test_structural_profile_and_top_k() -> None:
    frame = pl.DataFrame(
        {"amount": [*VALUES, None], "segment": ["SMB", "SMB", "Consumer", None, "SMB", "SMB"]}
    )
    profile = profile_frame(frame, "dsv_test", {})
    amount, segment = profile.columns
    assert profile.row_count == 6
    assert amount.null_ratio == pytest.approx(1 / 6)
    assert (amount.cardinality, amount.unique_ratio) == (5, 1.0)
    assert segment.categorical is not None
    assert not segment.categorical.masked
    top = segment.categorical.values[0]
    assert (top.value, top.count, top.frequency) == ("SMB", 4, 0.8)
    assert (segment.cardinality, segment.unique_ratio) == (2, 0.4)


def test_duplicate_rows_and_flags() -> None:
    frame = pl.DataFrame({"a": [1, 1, 2], "b": ["x", "x", "y"], "c": [None, None, None]})
    profile = profile_frame(frame, "dsv_test", {})
    assert profile.duplicate_rows == 1
    assert {"DUPLICATE_ROWS", "ALL_NULL_COLUMN:c"} <= set(profile_flags(profile))


def test_correlations_record_n_and_flag_near_duplicates() -> None:
    frame = pl.DataFrame({"a": [1.0, 2.0, 3.0, 4.0, None], "b": [2.0, 4.0, 6.0, 8.0, 1.0]})
    profile = profile_frame(frame, "dsv_test", {})
    pearson = next(p for p in profile.correlations if p.method == "pearson")
    assert pearson.n == 4
    assert pearson.coefficient == pytest.approx(1.0)
    assert "NEAR_DUPLICATE_COLUMNS:a|b" in profile_flags(profile)
    assert not profile.sampled


def test_correlations_use_a_recorded_sample_for_large_frames() -> None:
    frame = pl.DataFrame({"a": list(range(50)), "b": [x * 3 % 7 for x in range(50)]})
    profile = profile_frame(frame, "dsv_test", {}, sample_rows=20)
    assert (profile.sampled, profile.sample_size) == (True, 20)
    assert profile.correlations
    assert all(p.n == 20 for p in profile.correlations)
    assert profile.row_count == 50  # counts still use every row


@pytest.mark.parametrize(
    ("values", "grain", "regularity", "gaps"),
    [
        ([dt.date(2025, 1, d) for d in (1, 2, 3, 5)], "day", 2 / 3, 1),
        ([dt.date(2025, m, 1) for m in (1, 2, 3, 4, 6)], "month", 3 / 4, 1),
        ([dt.date(2025, 1, 1), dt.date(2025, 1, 4), dt.date(2025, 1, 14)], None, 0.0, 0),
    ],
)
def test_temporal_granularity(
    values: list[dt.date], grain: str | None, regularity: float, gaps: int
) -> None:
    stats = temporal_stats(pl.Series("d", values))
    assert stats is not None
    assert (stats.granularity, stats.gaps) == (grain, gaps)
    assert stats.regularity == pytest.approx(regularity)
    first = values[0]
    assert stats.min == dt.datetime(first.year, first.month, first.day, tzinfo=dt.UTC)


def test_pii_values_are_masked_in_persisted_profile() -> None:
    frame = pl.DataFrame(
        {
            "customer_email": ["ann@example.test", "bob@example.test", "ann@example.test"],
            "contact": ["+91 98765 43210", "+91 91234 56789", "+91 90000 00000"],
            "customer_id": ["C0001", "C0002", "C0001"],
            "segment": ["SMB", "SMB", "Consumer"],
        }
    )
    dumped = profile_frame(frame, "dsv_test", {}).model_dump_json()
    for raw in ("ann@example.test", "bob@", "98765", "C0001"):
        assert raw not in dumped
    by_name = {c.name: c for c in ProfileResult.model_validate_json(dumped).columns}
    for name in ("customer_email", "contact", "customer_id"):
        top_k = by_name[name].categorical
        assert top_k is not None
        assert top_k.masked
    email, segment = by_name["customer_email"].categorical, by_name["segment"].categorical
    assert email is not None
    assert segment is not None
    assert email.values[0].value == "a***"
    assert segment.values[0].value == "SMB"


def test_semantic_signals_are_signals_not_decisions() -> None:
    frame = pl.DataFrame({"orderDate": ["2025-01-01"], "net_amt": ["$5"]})
    profile = profile_frame(frame, "dsv_test", {})
    ingest: list[dict[str, object]] = [{"name": "net_amt", "unit_hint": "currency", "formats": []}]
    signals = SemanticSignalExtractor().extract(profile, ingest)
    assert signals["orderDate"].name_tokens == ["order", "date"]
    assert signals["orderDate"].regex_hit_ratios["date"] == 1.0
    assert signals["net_amt"].unit_hint == "currency"
    assert name_tokens("customerEmail_2") == ["customer", "email", "2"]


# --- PROFILE-010: invariants on edge cases (parametrized; no property-testing library present)

EDGE_FRAMES = {
    "all_null": pl.DataFrame(
        {"a": [None, None], "b": [None, None]}, schema={"a": pl.Float64, "b": pl.String}
    ),
    "single_row": pl.DataFrame({"a": [3.5], "b": ["x"], "d": [dt.date(2025, 1, 1)]}),
    "mixed_as_text": pl.DataFrame({"m": ["1", "two", "3", None]}),
    "empty": pl.DataFrame({"a": []}, schema={"a": pl.Int64}),
    "constant": pl.DataFrame({"a": [5, 5, 5, 5], "b": [1, 2, 3, 4]}),
    "negatives_zeros": pl.DataFrame({"a": [-1.0, 0.0, 0.0, 2.0, None]}),
}
MIXED_META = {"m": IngestColumn(physical_type="mixed", parse_success_ratio=0.5)}


@pytest.mark.parametrize("name", list(EDGE_FRAMES))
def test_profile_invariants(name: str) -> None:
    frame = EDGE_FRAMES[name]
    profile = profile_frame(frame, "dsv_test", MIXED_META)
    # Contract validators enforce ratio bounds, cardinality <= non-null rows, unique names and
    # sampled <=> sample_size; the JSON round trip re-runs them on the persisted form.
    assert ProfileResult.model_validate_json(profile.model_dump_json()) == profile
    assert profile.row_count == frame.height
    for column in profile.columns:
        non_null = frame[column.name].drop_nulls().len()
        assert column.cardinality <= non_null
        if column.numeric is not None:
            assert column.numeric.min <= column.numeric.median <= column.numeric.max
            quantiles = list(column.numeric.quantiles.values())
            assert quantiles == sorted(quantiles)
            assert 0 <= column.numeric.zero_ratio + column.numeric.neg_ratio <= 1
        if non_null == 0:
            assert column.numeric is None
            assert column.temporal is None
    assert all(-1 <= p.coefficient <= 1 and p.n >= 3 for p in profile.correlations)


def test_single_row_numeric_has_zero_spread_and_no_skew() -> None:
    stats = numeric_stats(pl.Series("x", [3.5]))
    assert stats is not None
    assert (stats.std, stats.var, stats.skew) == (0.0, 0.0, None)


def test_mixed_column_keeps_ingest_parse_ratio() -> None:
    column = profile_frame(EDGE_FRAMES["mixed_as_text"], "dsv_test", MIXED_META).columns[0]
    assert (column.physical_type, column.parse_success_ratio) == ("mixed", 0.5)
    assert column.text is not None
    assert column.numeric is None
