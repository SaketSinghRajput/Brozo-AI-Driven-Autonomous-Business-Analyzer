"""Shared helpers for the ML unit tests (no database).

``ingested`` runs the real ingestion pipeline on an evals fixture, so the ML code sees the
exact Parquet column types production stores. ``FakeUow`` records the SQL parameters an ML tool
would write (the model run insert) without a database.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest

from app.core.clock import FrozenClock
from app.core.ids import SequentialIds
from app.core.scope import WorkspaceScope
from app.data.ingestion.inspector import FileInspector, InspectionLimits
from app.data.ingestion.pipeline import ingest_file, parser_for
from app.infra.db import UnitOfWork
from app.infra.storage import LocalStorage, Storage
from tests.unit.data.conftest import FIXTURES, LIMITS

SCOPE = WorkspaceScope(workspace_id="ws_00ab12", principal_id="usr_ml")
VERSION = "dsv_ml1"


def ingest_to(storage: Storage, workdir: Path, relative: str, key: str) -> None:
    path = FIXTURES / relative
    facts = FileInspector(InspectionLimits(max_bytes=1 << 30)).inspect(path)
    workdir.mkdir(parents=True, exist_ok=True)
    ingest_file(
        facts,
        parser_for(facts, None, LIMITS),
        filename=path.name,
        limits=LIMITS,
        storage=storage,
        key=key,
        workdir=workdir,
    )


@pytest.fixture(scope="session")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], pl.DataFrame]:
    root = tmp_path_factory.mktemp("ml_fixtures")
    storage = LocalStorage(root / "store")
    cache: dict[str, pl.DataFrame] = {}

    def load(relative: str) -> pl.DataFrame:
        if relative not in cache:
            key = relative.replace("/", "_")
            ingest_to(storage, root / f"work_{key}", relative, key)
            with storage.open_read(key) as f:
                cache[relative] = pl.read_parquet(f)
        return cache[relative]

    return load


class FakeSession:
    def __init__(self) -> None:
        self.statements: list[dict[str, Any]] = []

    def execute(self, _statement: Any, params: dict[str, Any]) -> None:
        self.statements.append(params)


class FakeUow:
    def __init__(self) -> None:
        self.session = FakeSession()

    @contextmanager
    def __call__(self, _scope: Any) -> Iterator[FakeSession]:
        yield self.session


def tool_deps() -> tuple[UnitOfWork, FakeUow, SequentialIds, FrozenClock]:
    fake = FakeUow()
    clock = FrozenClock(datetime(2026, 9, 28, tzinfo=UTC))
    return cast(UnitOfWork, fake), fake, SequentialIds(), clock
