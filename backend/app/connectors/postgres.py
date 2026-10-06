"""PostgreSQL snapshot reader (TASK-DATA-013, CLAUDE.md sections 11 and 13).

The source database is untrusted and so is the user's query. Layers:
1. Validation: a table is one or two plain identifiers (quoted with `sql.Identifier`); a query
   is a single SELECT with no `;`, no comments, at most `MAX_QUERY_CHARS`.
2. The query only ever runs wrapped as `SELECT * FROM (<query>) AS snapshot LIMIT n`, which
   PostgreSQL rejects for anything but a single row-returning query (no DDL/DML, no second
   statement), through a server-side cursor (`DECLARE ... CURSOR FOR`, again one query).
3. The transaction is read-only twice over (`default_transaction_read_only` at connect and
   `SET TRANSACTION READ ONLY`), with `SET LOCAL statement_timeout` and a connect timeout.
4. Rows stream to a CSV file and stop at `max_rows` (exceeding it fails the import: a silently
   truncated table would mislead every analysis built on it).
Errors carry fixed messages and reason codes only; driver messages (which can echo host,
user or SQL) are never surfaced.
"""

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, TextIO

import psycopg
from psycopg import errors, sql

from app.core.errors import AppValidationError, DataError, SecurityError
from app.data.ingestion.errors import reject

MAX_QUERY_CHARS = 20_000
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_SELECT = re.compile(r"^select\b", re.IGNORECASE)


def table_parts(name: str) -> tuple[str, ...]:
    parts = tuple(name.split("."))
    if len(parts) > 2 or not all(_IDENT.match(p) for p in parts):
        raise reject(
            AppValidationError, "CONNECTOR_BAD_TABLE", "Use a table name like schema.table."
        )
    return parts


def select_query(query: str) -> str:
    text = query.strip().removesuffix(";").strip()
    if (
        not text
        or len(text) > MAX_QUERY_CHARS
        or not _SELECT.match(text)
        or any(token in text for token in (";", "--", "/*"))
    ):
        raise reject(
            AppValidationError,
            "CONNECTOR_QUERY_REJECTED",
            "Only a single SELECT statement without comments can be imported.",
        )
    return text


@dataclass(frozen=True)
class Target:
    host: str
    address: str  # resolved and SSRF-checked; libpq connects here (hostaddr)
    port: int
    database: str
    username: str
    sslmode: str


@dataclass(frozen=True)
class Limits:
    connect_timeout_s: int
    statement_timeout_ms: int
    max_rows: int


def _statement(table: str | None, query: str | None, max_rows: int) -> sql.Composed:
    limit = sql.Literal(max_rows + 1)
    if table is not None:
        source = sql.Identifier(*table_parts(table))
        return sql.SQL("SELECT * FROM {} LIMIT {}").format(source, limit)
    if query is None:
        raise AppValidationError("a table or a query is required")
    # select_query() rejected `;` and comments; the wrapper keeps it one row-returning SELECT.
    inner = sql.SQL(select_query(query))  # validated user SQL
    return sql.SQL("SELECT * FROM ({}) AS snapshot LIMIT {}").format(inner, limit)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes | memoryview):
        return "\\x" + bytes(value).hex()
    if isinstance(value, dict | list):
        return json.dumps(value, default=str)
    return str(value)


def snapshot_to_csv(
    target: Target,
    password: str,
    limits: Limits,
    out: TextIO,
    *,
    table: str | None = None,
    query: str | None = None,
) -> int:
    """Writes header + rows as CSV to `out`; returns the row count."""
    statement = _statement(table, query, limits.max_rows)
    try:
        with psycopg.connect(
            host=target.host,
            hostaddr=target.address,
            port=target.port,
            dbname=target.database,
            user=target.username,
            password=password,
            sslmode=target.sslmode,
            connect_timeout=limits.connect_timeout_s,
            application_name="analyzer-connector",
            options="-c default_transaction_read_only=on",
        ) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(
                sql.SQL("SET LOCAL statement_timeout = {}").format(
                    sql.Literal(limits.statement_timeout_ms)
                )
            )
            rows = 0
            with conn.cursor(name="snapshot") as cur:
                cur.itersize = 2_000
                cur.execute(statement)
                writer = csv.writer(out, lineterminator="\n")
                writer.writerow([column.name for column in cur.description or ()])
                for row in cur:
                    rows += 1
                    if rows > limits.max_rows:
                        raise reject(
                            DataError,
                            "ROW_CAP_EXCEEDED",
                            f"The source has more than {limits.max_rows} rows.",
                            limit_rows=limits.max_rows,
                        )
                    writer.writerow([_cell(v) for v in row])
            conn.rollback()  # read-only: nothing to commit
            return rows
    except errors.QueryCanceled:  # before OperationalError: it is a subclass
        raise reject(
            DataError, "CONNECTOR_TIMEOUT", "The source query exceeded the statement timeout."
        ) from None
    except errors.ReadOnlySqlTransaction:
        raise reject(
            SecurityError, "CONNECTOR_READ_ONLY_VIOLATION", "The source query tried to write."
        ) from None
    except psycopg.OperationalError:
        unreachable = reject(DataError, "CONNECTOR_UNREACHABLE", "Could not connect to the source.")
        unreachable.retryable = True
        raise unreachable from None
    except psycopg.Error as exc:
        raise reject(
            DataError,
            "CONNECTOR_QUERY_FAILED",
            "The source rejected the query.",
            sqlstate=exc.sqlstate or "",
        ) from None
