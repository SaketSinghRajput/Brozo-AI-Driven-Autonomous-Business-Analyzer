from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import AppConfig, Environment
from app.main import create_app


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        _env_file=None,
        env=Environment.TEST,
        database={
            "url": "postgresql+psycopg://test:test@127.0.0.1:5432/test",
            "check_schema_on_startup": False,
        },
        storage={"datastore_path": tmp_path / "datastore"},
        security={"session_secret": "t" * 32},
    )


@pytest.fixture(scope="session")
def _span_store() -> InMemorySpanExporter:
    # The global tracer provider can be set once per process; every test shares this one.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(_span_store: InMemorySpanExporter) -> InMemorySpanExporter:
    """Finished spans recorded by app.core.observability during one test."""
    _span_store.clear()
    return _span_store


@pytest.fixture
def client(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), raise_server_exceptions=False) as c:
        yield c
