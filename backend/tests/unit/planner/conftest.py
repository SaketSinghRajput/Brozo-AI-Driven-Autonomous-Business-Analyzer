"""Fixture datasets understood through the production path, without a database.

CSV -> ingestion pipeline (Parquet) -> profiler -> SchemaAnalyzer (understand_frame, as the
dataset pipeline runs it) -> DomainDetector -> SemanticMapper -> DataFacts. Cached per session.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from app.data.domain import ParseLimits
from app.data.ingestion.inspector import FileInspector, InspectionLimits
from app.data.ingestion.pipeline import PipelineLimits, ingest_file, parser_for
from app.data.profiling.profiler import profile_frame
from app.data.profiling.service import ingest_columns
from app.data.semantic.mapper import SemanticMapper
from app.data.semantic.overrides import remap
from app.data.understanding.analyzer import SchemaAnalyzer
from app.data.understanding.domain import DomainClassification
from app.data.understanding.domain_detector import DomainDetector
from app.data.understanding_pipeline import understand_frame
from app.infra.storage import LocalStorage
from app.planner.facts import DataFacts, build_facts

FIXTURES = Path(__file__).resolve().parents[4] / "evals" / "fixtures"
LIMITS = PipelineLimits(
    ParseLimits(max_rows=1_000_000, max_reject_ratio=0.25, max_field_chars=100_000),
    batch_rows=5_000,
)
TABLES = {
    "retail": "orders",
    "saas": "subscriptions",
    "agriculture": "production",
    "insufficient": "sales",
    "ambiguous": "data",
}


@dataclass(frozen=True)
class Understood:
    facts: DataFacts
    domain: DomainClassification
    labels: dict[str, Any]


def understand(name: str, tmp: Path) -> Understood:
    table = TABLES[name]
    path = FIXTURES / name / f"{table}.csv"
    storage = LocalStorage(tmp / "store")
    workdir = tmp / "work"
    workdir.mkdir()
    file_facts = FileInspector(InspectionLimits(max_bytes=1 << 30)).inspect(path)
    report = ingest_file(
        file_facts,
        parser_for(file_facts, None, LIMITS),
        filename=path.name,
        limits=LIMITS,
        storage=storage,
        key="v.parquet",
        workdir=workdir,
    )
    with storage.open_read("v.parquet") as f:
        frame = pl.read_parquet(f)
    columns = ingest_columns(report.model_dump(mode="json"))
    profile = profile_frame(frame, f"dsv_{name}", columns)
    understanding = understand_frame(SchemaAnalyzer(), table, profile, frame)
    domain = DomainDetector().detect([understanding])
    model = remap(SemanticMapper(), understanding, domain, table=table, overrides=[]).model
    labels = json.loads((FIXTURES / name / "labels.json").read_text(encoding="utf-8"))
    return Understood(build_facts(understanding, model, profile), domain, labels)


@pytest.fixture(scope="session")
def understood(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Understood]:
    cache: dict[str, Understood] = {}

    def get(name: str) -> Understood:
        if name not in cache:
            cache[name] = understand(name, tmp_path_factory.mktemp(name))
        return cache[name]

    return get
