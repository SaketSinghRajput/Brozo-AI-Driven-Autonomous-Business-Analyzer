"""Shared helpers for the ingestion/profiling unit tests (no database)."""

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from app.data.domain import ParseLimits
from app.data.ingestion.inspector import FileInspector, InspectionLimits
from app.data.ingestion.pipeline import IngestReport, PipelineLimits, ingest_file, parser_for
from app.infra.storage import LocalStorage

REPO = Path(__file__).resolve().parents[4]
FIXTURES = REPO / "evals" / "fixtures"
LIMITS = PipelineLimits(
    ParseLimits(max_rows=1_000_000, max_reject_ratio=0.25, max_field_chars=100_000),
    batch_rows=1_000,
)

Ingest = Callable[..., tuple[IngestReport, LocalStorage, str]]


def generator_module() -> ModuleType:
    """scripts/generate_fixtures.py (zip bomb / oversized generators are not committed)."""
    spec = importlib.util.spec_from_file_location("gen", REPO / "scripts" / "generate_fixtures.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ingest(tmp_path: Path) -> Ingest:
    storage = LocalStorage(tmp_path / "store")
    counter = iter(range(1_000))

    def run(
        path: Path,
        *,
        filename: str | None = None,
        sheet: str | None = None,
        limits: PipelineLimits = LIMITS,
        max_bytes: int = 1 << 30,
    ) -> tuple[IngestReport, LocalStorage, str]:
        n = next(counter)
        workdir = tmp_path / f"work{n}"
        workdir.mkdir()
        facts = FileInspector(InspectionLimits(max_bytes=max_bytes)).inspect(path)
        key = f"v{n}.parquet"
        report = ingest_file(
            facts,
            parser_for(facts, sheet, limits),
            filename=filename or path.name,
            limits=limits,
            storage=storage,
            key=key,
            workdir=workdir,
        )
        return report, storage, key

    return run
