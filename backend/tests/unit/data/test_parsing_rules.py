"""TASK-DATA-003/005/006/007: parser, normalizer and type-inference rules on synthetic files."""

import codecs
import tracemalloc
from pathlib import Path

import polars as pl
import pytest

from app.core.errors import DataError
from app.data.domain import ParseLimits
from app.data.ingestion.csv_parser import looks_like_header, sniff_delimiter
from app.data.ingestion.errors import reason_of
from app.data.ingestion.normalizer import normalize_headers
from app.data.ingestion.pipeline import PipelineLimits

from .conftest import FIXTURES, Ingest


def _csv(tmp_path: Path, text: str, name: str = "t.csv", encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def _typed(ingest: Ingest, path: Path) -> tuple[dict[str, str], pl.DataFrame, list[str]]:
    report, storage, key = ingest(path)
    with storage.open_read(key) as f:
        frame = pl.read_parquet(f)
    return {c.name: c.physical_type for c in report.columns}, frame, report.flags


@pytest.mark.parametrize(
    ("sample", "delimiter"),
    [("a,b,c\n1,2,3\n", ","), ("a;b;c\n1;2;3\n", ";"), ("a\tb\n1\t2\n", "\t"), ("a|b\n1|2", "|")],
)
def test_delimiter_sniffing(sample: str, delimiter: str) -> None:
    assert sniff_delimiter(sample) == delimiter


def test_quoted_delimiters_do_not_split_fields(ingest: Ingest, tmp_path: Path) -> None:
    types, frame, _ = _typed(ingest, _csv(tmp_path, 'name,amount\n"Doe, John","1,234.50"\nA,2\n'))
    assert frame["name"][0] == "Doe, John"
    assert types["amount"] == "float"
    assert frame["amount"].to_list() == [1234.5, 2.0]


def test_header_detection() -> None:
    assert looks_like_header(["id", "amount", "amount"])
    assert not looks_like_header(["1", "2025-01-05", "3.5"])
    assert not looks_like_header(["name", ""])


def test_headerless_file_gets_generated_names(ingest: Ingest, tmp_path: Path) -> None:
    report, _, _ = ingest(_csv(tmp_path, "1,2025-01-05,3.5\n2,2025-01-06,4.5\n"))
    assert [c.name for c in report.columns] == ["column_1", "column_2", "column_3"]
    assert "NO_HEADER_ROW" in report.flags
    assert report.rows_ingested == 2


def test_header_normalization_keeps_source_names() -> None:
    names, flags = normalize_headers(["  Net   Amount ", "", "net amount", "id\x00"])
    assert [(n.name, n.source_name) for n in names] == [
        ("Net Amount", "  Net   Amount "),
        ("column_2", ""),
        ("net amount_2", "net amount"),
        ("id", "id\x00"),
    ]
    assert flags == ["DUPLICATE_COLUMN_NAMES:net amount", "EMPTY_COLUMN_NAME:column_2"]


@pytest.mark.parametrize(
    ("bom", "encoding"), [(codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le")]
)
def test_bom_files(ingest: Ingest, tmp_path: Path, bom: bytes, encoding: str) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(bom + "city,n\nZürich,1\n".encode(encoding))
    report, storage, key = ingest(path)
    assert [c.name for c in report.columns] == ["city", "n"]  # no BOM residue in the header
    with storage.open_read(key) as f:
        assert pl.read_parquet(f)["city"][0] == "Zürich"


def test_currency_percent_and_locale_rules(ingest: Ingest, tmp_path: Path) -> None:
    text = (
        "price,discount,eu,ambiguous,zip,huge,flag\n"
        '"$1,200.50",10%,"1.234,5","1,234",00123,1234567890123456789012,yes\n'
        '€ 99.00,5 %,"7,25","2,500",04567,1,no\n'
    )
    types, frame, flags = _typed(ingest, _csv(tmp_path, text))
    assert types == {
        "price": "float",
        "discount": "float",  # percent cells are stored as fractions (unit pct, TRD 25.1)
        "eu": "float",
        "ambiguous": "string",
        "zip": "string",
        "huge": "string",
        "flag": "bool",
    }
    assert frame["price"].to_list() == [1200.5, 99.0]
    assert frame["discount"].to_list() == [0.10, 0.05]
    assert frame["eu"].to_list() == [1234.5, 7.25]
    assert frame["zip"].to_list() == ["00123", "04567"]  # leading zeros are codes, kept verbatim
    assert "AMBIGUOUS_NUMBER_LOCALE:ambiguous" in flags
    assert "DECIMAL_COMMA:eu" in flags


def test_percent_named_columns_in_points_become_fractions(ingest: Ingest, tmp_path: Path) -> None:
    """GAPS-1/B7: "discount_pct" = 10 means 10 % (the retail fixture): stored as 0.10 with the
    percent hint and a review flag. A percent column already in [-1, 1] is left as fractions;
    other numeric columns are untouched."""
    text = "discount_pct,share_pct,growth percent,units\n10,0.25,-12.5,10\n5,0.5,150,5\n0,1,0,0\n"
    report, storage, key = ingest(_csv(tmp_path, text))
    with storage.open_read(key) as f:
        frame = pl.read_parquet(f)
    assert frame["discount_pct"].to_list() == [0.10, 0.05, 0.0]
    assert frame["share_pct"].to_list() == [0.25, 0.5, 1.0]
    assert frame["growth percent"].to_list() == [-0.125, 1.5, 0.0]
    assert frame["units"].to_list() == [10, 5, 0]
    hints = {c.name: c.unit_hint for c in report.columns}
    assert hints == {
        "discount_pct": "percent",
        "share_pct": None,
        "growth percent": "percent",
        "units": None,
    }
    assert "PERCENT_SCALED_FROM_HEADER:discount_pct" in report.flags


def test_ambiguous_and_resolved_day_month_order(ingest: Ingest, tmp_path: Path) -> None:
    text = "a,b,c\n01/02/2025,13/02/2025,02/13/2025\n03/04/2025,01/03/2025,03/01/2025\n"
    types, frame, flags = _typed(ingest, _csv(tmp_path, text))
    assert types == {"a": "string", "b": "date", "c": "date"}
    assert "DATE_ORDER_AMBIGUOUS:a" in flags
    assert {"DATE_FORMAT_DMY:b", "DATE_FORMAT_MDY:c"} <= set(flags)
    assert str(frame["b"][0]) == "2025-02-13"
    assert str(frame["c"][0]) == "2025-02-13"


def test_datetimes_with_offsets_are_normalized_to_utc(ingest: Ingest, tmp_path: Path) -> None:
    text = "ts\n2025-01-05T10:00:00+05:30\n2025-01-05T10:00:00Z\n2025-01-05 10:00:00\n"
    report, storage, key = ingest(_csv(tmp_path, text))
    column = report.columns[0]
    assert (column.physical_type, column.tz_mixed) == ("datetime", True)
    assert "TIMEZONE_ASSUMED_UTC:ts" in report.flags
    with storage.open_read(key) as f:
        values = pl.read_parquet(f)["ts"].to_list()
    assert [v.hour for v in values] == [4, 10, 10]


def test_mixed_column_is_kept_as_text_losslessly(ingest: Ingest, tmp_path: Path) -> None:
    report, storage, key = ingest(_csv(tmp_path, "v\n1\n2\n3\nabc\n"))
    column = report.columns[0]
    assert (column.physical_type, column.parse_success_ratio) == ("mixed", 0.75)
    assert "MIXED_TYPES_IN_COLUMN:v" in report.flags
    with storage.open_read(key) as f:
        assert pl.read_parquet(f)["v"].to_list() == ["1", "2", "3", "abc"]


def test_invalid_calendar_dates_are_not_coerced(ingest: Ingest, tmp_path: Path) -> None:
    types, frame, _ = _typed(ingest, _csv(tmp_path, "d\n2025-02-28\n2025-02-30\n"))
    assert types["d"] == "mixed"  # 1 of 2 parse as a date: text + flag, the bad date is kept
    assert frame["d"].to_list() == ["2025-02-28", "2025-02-30"]


def test_too_many_bad_rows_rejects_the_file(ingest: Ingest, tmp_path: Path) -> None:
    with pytest.raises(DataError) as info:
        ingest(_csv(tmp_path, "a,b\n1,2\n1\n1\n1,2,3\n"))
    assert reason_of(info.value) == "TOO_MANY_BAD_ROWS"


def test_row_limit(ingest: Ingest, tmp_path: Path) -> None:
    limits = PipelineLimits(ParseLimits(max_rows=2, max_reject_ratio=0.5, max_field_chars=100))
    with pytest.raises(DataError) as info:
        ingest(_csv(tmp_path, "a\n1\n2\n3\n"), limits=limits)
    assert reason_of(info.value) == "TOO_MANY_ROWS"


def test_header_only_file(ingest: Ingest, tmp_path: Path) -> None:
    with pytest.raises(DataError) as info:
        ingest(_csv(tmp_path, "a,b\n"))
    assert reason_of(info.value) == "NO_DATA_ROWS"


def test_unknown_sheet(ingest: Ingest) -> None:
    with pytest.raises(DataError) as info:
        ingest(FIXTURES / "hostile" / "formulas_cached.xlsx", sheet="Nope")
    assert reason_of(info.value) == "SHEET_NOT_FOUND"
    assert info.value.details
    assert info.value.details["sheets"] == ["Sheet1"]


def test_selected_sheet_is_recorded(ingest: Ingest) -> None:
    report, _, _ = ingest(FIXTURES / "hostile" / "formulas_cached.xlsx", sheet="Sheet1")
    assert (report.notes["sheet"], report.sheets) == ("Sheet1", ["Sheet1"])


def test_same_input_gives_identical_stored_bytes(ingest: Ingest) -> None:
    path = FIXTURES / "retail" / "products.csv"
    _, storage, first = ingest(path)
    _, _, second = ingest(path)
    with storage.open_read(first) as a, storage.open_read(second) as b:
        assert a.read() == b.read()


@pytest.mark.slow
def test_csv_parsing_memory_is_bounded_by_the_batch(ingest: Ingest, tmp_path: Path) -> None:
    """200k rows (~7 MB) with 1k-row batches: the Python-heap peak stays far below the data."""
    path = tmp_path / "large.csv"
    with path.open("w", encoding="utf-8") as f:
        f.write("id,category,value,date\n")
        for i in range(200_000):
            f.write(f"{i},cat{i % 7},{i * 0.5:.2f},2025-01-{1 + i % 28:02d}\n")
    tracemalloc.start()
    try:
        report, _, _ = ingest(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.rows_ingested == 200_000
    # All rows held as Python lists of str would need well over 50 MB of Python heap.
    assert peak < 20 * 1024 * 1024
