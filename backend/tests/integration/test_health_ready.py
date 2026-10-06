"""TASK-INFRA-004 / TASK-MLOPS-002 against a real database: readiness, and the request's trace id
reaching the job row and the worker's span."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.observability import otel_trace_id
from app.jobs.models import JobType
from app.jobs.worker import Worker
from app.security.audit import make_verify_handler
from app.security.bootstrap import bootstrap_admin

from .auth_helpers import PASSWORD, container_of, csrf_headers, login, make_app
from .conftest import TestDatabase


@pytest.fixture
def api(database: TestDatabase, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        make_app(database, tmp_path), base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield client


def test_ready_when_database_and_queue_are_up(api: TestClient) -> None:
    r = api.get("/api/v1/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] is True
    assert body["job_queue"] is True
    assert body["status"] == ("ready" if body["llm"] else "degraded")


def test_request_trace_id_reaches_job_and_worker_span(
    api: TestClient, spans: InMemorySpanExporter
) -> None:
    container = container_of(api)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.test"
    bootstrap_admin(container, email, PASSWORD, "Trace")
    csrf = login(api, email)
    trace = uuid.uuid4().hex
    r = api.post("/api/v1/audit/verify", headers={**csrf_headers(csrf), "X-Trace-Id": trace})
    assert r.status_code == 202
    assert r.json()["trace_id"] == trace  # api -> job row

    handler = make_verify_handler(container.uow, container.auth.audit_keys)
    worker = Worker(container.uow, {JobType.AUDIT_VERIFY.value: handler})
    assert worker.run_once()
    finished = spans.get_finished_spans()
    job_spans = [s for s in finished if s.name == "job.run"]
    assert job_spans
    assert job_spans[-1].context.trace_id == otel_trace_id(trace)  # job -> worker span
    http_spans = [s for s in finished if s.name == "http.request"]
    assert any(s.context.trace_id == otel_trace_id(trace) for s in http_spans)
