"""Analytical SQL engine adapter (TASK-SQL-001, TASK-SQL-005; ADR-0004, BENCH-001).

One fresh in-memory DuckDB connection per query, configured at connect time with external
access, extension auto-install/auto-load and community extensions disabled, no temp directory
(no spilling to disk) and Python replacement scans off (so SQL cannot reach Python variables).
The dataset version's Arrow table is handed over with ``con.register`` (Mode A): DuckDB itself
never touches the filesystem. Then ``SET lock_configuration=true`` freezes every setting.

``allowed_directories`` is deliberately not used: RES-014 found it also permits ``COPY ... TO``.
Limits: wall-clock timeout via a watchdog calling ``con.interrupt()``, a row cap (one extra row
is fetched to detect truncation) and ``memory_limit``/``threads`` fixed before the lock.
"""

import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import duckdb
import pyarrow as pa

from app.core.errors import ToolError

log = logging.getLogger("app.analytics.engine")

ENGINE_VERSION = f"duckdb@{duckdb.__version__}"
_VIEW_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
Param = str | int | float | bool | date | None


class QueryLimitError(ToolError):
    """A query exceeded a resource limit. Deterministic, so not retryable."""


@dataclass(frozen=True, slots=True)
class QueryLimits:
    timeout_s: float = 10.0
    max_rows: int = 10_000
    memory_limit_mb: int = 2048  # BENCH-001: conservative per-connection budget
    threads: int = 2

    def __post_init__(self) -> None:
        if self.timeout_s <= 0 or self.max_rows < 1 or self.memory_limit_mb < 16:
            raise ValueError("invalid query limits")
        if self.threads < 1:
            raise ValueError("threads must be >= 1")


@dataclass(frozen=True, slots=True)
class RawResult:
    schema: pa.Schema
    rows: list[tuple[object, ...]]
    truncated: bool


def _config(limits: QueryLimits) -> dict[str, str | bool | int | float | list[str]]:
    return {
        "enable_external_access": False,
        "autoload_known_extensions": False,
        "autoinstall_known_extensions": False,
        "allow_community_extensions": False,
        "allow_unsigned_extensions": False,
        "python_enable_replacements": False,
        "temp_directory": "",
        "memory_limit": f"{limits.memory_limit_mb}MB",
        "threads": limits.threads,
    }


class AnalyticalEngine:
    def __init__(self, limits: QueryLimits | None = None) -> None:
        self.limits = limits or QueryLimits()

    def connect(self, views: Mapping[str, pa.Table]) -> duckdb.DuckDBPyConnection:
        """A locked connection that sees only ``views``. The caller must close it."""
        if not views:
            raise ValueError("at least one view is required")
        con = duckdb.connect(":memory:", config=_config(self.limits))
        try:
            for name, table in views.items():
                if not _VIEW_NAME.match(name):
                    raise ValueError(f"invalid view name {name!r}")
                con.register(name, table)
            con.execute("SET lock_configuration=true")
        except BaseException:
            con.close()
            raise
        return con

    def execute(
        self,
        views: Mapping[str, pa.Table],
        sql: str,
        params: Mapping[str, Param],
        max_rows: int | None = None,
    ) -> RawResult:
        cap = min(max_rows or self.limits.max_rows, self.limits.max_rows)
        con = self.connect(views)
        watchdog = threading.Timer(self.limits.timeout_s, con.interrupt)
        watchdog.daemon = True
        watchdog.start()
        try:
            cursor = con.execute(sql, dict(params))
            reader = cursor.to_arrow_reader(batch_size=min(cap + 1, 8192))
            rows: list[tuple[object, ...]] = []
            for batch in reader:
                rows.extend(zip(*(col.to_pylist() for col in batch.columns), strict=True))
                if len(rows) > cap:
                    break
            schema = reader.schema
        except duckdb.InterruptException:
            raise QueryLimitError("query exceeded the time limit") from None
        except duckdb.OutOfMemoryException:
            raise QueryLimitError("query exceeded the memory limit") from None
        except duckdb.Error as exc:
            log.warning("analytics.query_failed", extra={"fields": {"type": type(exc).__name__}})
            raise ToolError("query failed") from None
        finally:
            watchdog.cancel()
            con.close()
        return RawResult(schema=schema, rows=rows[:cap], truncated=len(rows) > cap)
