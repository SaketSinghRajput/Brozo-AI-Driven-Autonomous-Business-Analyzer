"""TASK-SQL-002 / TASK-SQL-005: every known escape from the analytical engine is blocked, and
the resource limits hold. Each statement runs on a locked connection built exactly as queries
are (``AnalyticalEngine.connect``)."""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from app.analytics.sql.engine import AnalyticalEngine, QueryLimitError, QueryLimits
from app.core.errors import ToolError

VIEWS = {"data": pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})}

ESCAPES = {
    "read_csv": "SELECT * FROM read_csv('secrets.csv')",
    "read_csv_auto": "SELECT * FROM read_csv_auto('secrets.csv')",
    "read_parquet": "SELECT * FROM read_parquet('other_tenant.parquet')",
    "read_json": "SELECT * FROM read_json('x.json')",
    "read_text": "SELECT * FROM read_text('C:/Windows/win.ini')",
    "read_blob": "SELECT * FROM read_blob('/etc/passwd')",
    "glob": "SELECT * FROM glob('*')",
    "sniff_csv": "SELECT * FROM sniff_csv('x.csv')",
    "replacement_scan_file": "SELECT * FROM 'orders.csv'",
    "http_table_function": "SELECT * FROM read_csv('http://127.0.0.1:9/x.csv')",
    "https_parquet": "SELECT * FROM read_parquet('https://example.com/x.parquet')",
    "attach_file": "ATTACH 'stolen.db' AS stolen",
    "copy_to": "COPY data TO 'exfil.csv'",
    "copy_select_to": "COPY (SELECT * FROM data) TO 'exfil.parquet' (FORMAT parquet)",
    "export_database": "EXPORT DATABASE 'dump_dir'",
    "install": "INSTALL httpfs",
    "load": "LOAD httpfs",
    "load_path": "LOAD 'evil.duckdb_extension'",
    "set_external_access": "SET enable_external_access=true",
    "reset_lock": "RESET lock_configuration",
    "unlock": "SET lock_configuration=false",
    "set_memory": "SET memory_limit='64GB'",
    "set_threads": "SET threads=64",
    "set_replacements": "SET python_enable_replacements=true",
    "pragma_memory": "PRAGMA memory_limit='64GB'",
    "set_temp_dir": "SET temp_directory='.'",
}


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = AnalyticalEngine(QueryLimits(memory_limit_mb=64, threads=1)).connect(VIEWS)
    yield connection
    connection.close()


@pytest.mark.parametrize("sql", ESCAPES.values(), ids=ESCAPES.keys())
def test_escape_is_blocked(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    with pytest.raises(duckdb.Error):
        con.execute(sql).fetchall()


def test_blocked_writes_leave_no_files(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    target = (tmp_path / "exfil.csv").as_posix()
    with pytest.raises(duckdb.Error):
        con.execute(f"COPY data TO '{target}'")
    assert not (tmp_path / "exfil.csv").exists()


def test_python_variables_are_not_reachable(con: duckdb.DuckDBPyConnection) -> None:
    other_tenant = pa.table({"secret": [1]})  # a local variable the SQL must not see
    assert other_tenant.num_rows == 1
    with pytest.raises(duckdb.CatalogException):
        con.execute("SELECT * FROM other_tenant").fetchall()


def test_only_registered_views_are_visible(con: duckdb.DuckDBPyConnection) -> None:
    assert con.execute("SELECT sum(a) FROM data WHERE b <> $v", {"v": "y"}).fetchone() == (4,)
    views = con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()
    assert {r[0] for r in views} == {"data"}
    assert con.execute("SELECT count(*) FROM duckdb_tables()").fetchone() == (0,)


def test_settings_are_locked_to_safe_values(con: duckdb.DuckDBPyConnection) -> None:
    def setting(name: str) -> object:
        row = con.execute("SELECT current_setting($n)", {"n": name}).fetchone()
        assert row is not None
        return row[0]

    assert setting("enable_external_access") is False
    assert setting("lock_configuration") is True
    assert setting("autoinstall_known_extensions") is False
    assert setting("autoload_known_extensions") is False
    assert setting("python_enable_replacements") is False


def test_invalid_view_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid view name"):
        AnalyticalEngine().connect({'x"; DROP': VIEWS["data"]})


def test_timeout_interrupts_the_query() -> None:
    engine = AnalyticalEngine(QueryLimits(timeout_s=0.2))
    with pytest.raises(QueryLimitError, match="time limit"):
        engine.execute(VIEWS, "SELECT count(*) FROM range(100000000000) t(i), data", {})


def test_memory_cap_is_enforced() -> None:
    engine = AnalyticalEngine(QueryLimits(memory_limit_mb=32, threads=1))
    with pytest.raises(QueryLimitError, match="memory limit"):
        engine.execute(VIEWS, "SELECT list(i) FROM range(100000000) t(i)", {})


def test_row_cap_sets_truncated() -> None:
    engine = AnalyticalEngine(QueryLimits(max_rows=2))
    capped = engine.execute(VIEWS, "SELECT a FROM data ORDER BY a", {})
    assert (capped.rows, capped.truncated) == ([(1,), (2,)], True)
    exact = engine.execute(VIEWS, "SELECT a FROM data ORDER BY a", {}, max_rows=3)
    assert (len(exact.rows), exact.truncated) == (2, True)  # the engine cap (2) wins
    empty = AnalyticalEngine().execute(VIEWS, "SELECT a FROM data WHERE a > 5", {})
    assert (empty.rows, empty.truncated, empty.schema.names) == ([], False, ["a"])


def test_sql_errors_are_user_safe() -> None:
    with pytest.raises(ToolError) as info:
        AnalyticalEngine().execute(VIEWS, "SELECT nope FROM data", {})
    assert info.value.message == "query failed"  # no SQL or engine text leaks
