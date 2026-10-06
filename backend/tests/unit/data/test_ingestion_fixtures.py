"""TASK-DATA-003/006/007/014: the fixture corpus ingests with the labelled outcome."""

import json
from pathlib import Path

import polars as pl
import pytest

from .conftest import FIXTURES, Ingest

LABELLED = [
    ("retail", "orders"),
    ("retail", "customers"),
    ("retail", "products"),
    ("saas", "subscriptions"),
    ("saas", "marketing_spend"),
    ("agriculture", "production"),
    ("insufficient", "sales"),
    ("ambiguous", "data"),
]


def _labels(family: str) -> dict[str, object]:
    labels: dict[str, object] = json.loads((FIXTURES / family / "labels.json").read_text("utf-8"))
    return labels


@pytest.mark.parametrize(("family", "table"), LABELLED)
def test_labelled_tables_ingest_every_row(ingest: Ingest, family: str, table: str) -> None:
    report, storage, key = ingest(FIXTURES / family / f"{table}.csv")
    tables = _labels(family)["tables"]
    assert isinstance(tables, dict)
    expected = tables[table]
    assert report.rows_ingested == expected["row_count"]
    assert report.rows_rejected == 0
    assert {c.name for c in report.columns} == set(expected["columns"])  # labels keys are sorted
    with storage.open_read(key) as f:
        assert pl.read_parquet(f).height == expected["row_count"]


def test_time_columns_are_typed_as_dates(ingest: Ingest) -> None:
    orders, _, _ = ingest(FIXTURES / "retail" / "orders.csv")
    spend, _, _ = ingest(FIXTURES / "saas" / "marketing_spend.csv")
    types = {c.name: c.physical_type for c in orders.columns + spend.columns}
    assert types["order_date"] == "date"
    assert types["month"] == "date"  # "2024-09" month grain → first of the month
    assert types["quantity"] == "int"
    assert types["unit_price"] == "float"
    assert types["order_id"] == "string"


def test_duplicate_headers_mixed_dates_and_null_tokens(ingest: Ingest) -> None:
    report, storage, key = ingest(FIXTURES / "messy" / "duplicate_headers_mixed_types.csv")
    assert report.rows_ingested == 49
    assert {
        "DUPLICATE_COLUMN_NAMES:amount",
        "MISSING_VALUE_TOKENS",
        "MIXED_DATE_FORMATS:date",
        "DATE_ORDER_AMBIGUOUS:date",  # 05/01/2025 is never disambiguated → text, not a guess
    } <= set(report.flags)
    columns = {c.name: c for c in report.columns}
    assert columns["amount_2"].source_name == "amount"
    assert columns["date"].physical_type == "string"
    with storage.open_read(key) as f:
        frame = pl.read_parquet(f)
    assert frame["amount"].null_count() > 0  # "NA", "null", "" became NULL, not text
    assert frame["date"].to_list()[:3] == ["2025-01-05", "05/01/2025", "Jan 5, 2025"]


def test_semicolon_decimal_comma_and_dmy_dates(ingest: Ingest) -> None:
    report, storage, key = ingest(FIXTURES / "messy" / "latin1_semicolon.csv")
    assert report.notes["delimiter"] == ";"
    assert {
        "DELIMITER_NOT_COMMA",
        "DECIMAL_COMMA:prix",
        "THOUSANDS_SEPARATOR:prix",
        "DATE_FORMAT_DMY:date",
    } <= set(report.flags)
    with storage.open_read(key) as f:
        frame = pl.read_parquet(f)
    assert frame["prix"].to_list()[:3] == [1234.56, 89.9, 15.0]
    assert str(frame["date"][0]) == "2025-02-19"


def test_non_utf8_file_is_decoded_and_flagged(ingest: Ingest, tmp_path: Path) -> None:
    # The committed "latin1" fixture is pure ASCII (generator bug), so use real latin-1 bytes.
    path = tmp_path / "fr.csv"
    rows = ["id;produit;prix"] + [f"{i};Crème brûlée n°{i};{i},50" for i in range(1, 30)]
    path.write_bytes("\n".join(rows).encode("latin-1"))
    report, storage, key = ingest(path)
    assert "ENCODING_NON_UTF8" in report.flags
    with storage.open_read(key) as f:
        assert pl.read_parquet(f)["produit"][0] == "Crème brûlée n°1"


def test_malformed_rows_are_quarantined_not_dropped_silently(ingest: Ingest) -> None:
    report, _, _ = ingest(FIXTURES / "messy" / "malformed_rows.csv")
    assert (report.rows_seen, report.rows_ingested, report.rows_rejected) == (30, 26, 4)
    assert "MALFORMED_ROW_FIELD_COUNT" in report.flags
    # ids 9005, 9012, 9020, 9025 (labels.json) are data rows 6, 13, 21, 26.
    assert [r.row_number for r in report.rejected_sample] == [6, 13, 21, 26]
    assert all("EXTRA" not in r.reason and "90" not in r.reason for r in report.rejected_sample)
