"""TASK-MLOPS-001/002: one trace id from the API request through the job to LLM and tool spans;
metrics carry no ids or raw paths."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, ValidationError

from app.core.config import AppConfig, ObservabilityConfig
from app.core.observability import otel_trace_id
from app.evidence.recorder import CallKey
from app.jobs.models import Job, JobStatus, JobType
from app.jobs.worker import JobContext, Worker
from app.llm.context import BuiltContext
from app.llm.fake import FakeLLMProvider
from app.llm.gateway import LLMGateway
from app.llm.ports import ChatResponse, LLMTask, Message, Usage
from app.llm.prompts import PromptRegistry
from app.llm.router import ModelProfile, ModelRouter
from app.main import create_app
from app.tools.domain import ToolContext
from app.tools.executor import ToolExecutor, TurnBudget
from app.tools.registry import ToolRegistry

TRACE = "0af7651916cd43dd8448eb211c80319c"
CHAIN = ("http.request", "job.run", "llm.call", "tool.unknown.tool")


class FakeUow:
    """Stands in for UnitOfWork: every repository write succeeds against a mock session."""

    @contextmanager
    def __call__(self, scope: Any) -> Iterator[MagicMock]:
        yield MagicMock()


class Answer(BaseModel):
    text: str


def _gateway() -> LLMGateway:
    fake = FakeLLMProvider(
        [
            ChatResponse(
                text='{"text":"hi"}',
                model_version="fake-1",
                usage=Usage(prompt_tokens=5, completion_tokens=2),
                finish_reason="stop",
            )
        ]
    )
    profile = ModelProfile(
        name="default",
        provider="fake",
        model_version="fake-1",
        max_context=4096,
        capabilities=fake.capabilities,
    )
    return LLMGateway(ModelRouter({"default": profile}), {"default": fake}, PromptRegistry())


def _job(trace_id: str) -> Job:
    now = datetime.now(UTC)
    return Job(
        id="job_1",
        workspace_id="ws_abcdef",
        type=JobType.AUDIT_VERIFY,
        payload={},
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=1,
        run_after=now,
        idempotency_key=None,
        cancel_requested=False,
        progress=0,
        result_ref=None,
        error=None,
        trace_id=trace_id,
        created_at=now,
        updated_at=now,
    )


def test_one_trace_from_request_through_job_to_llm_and_tool(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    # 1. API: the request's trace id is what the enqueue path copies into jobs.trace_id
    #    (covered against a real DB in tests/integration/test_health_ready.py).
    r = client.get("/api/v1/health", headers={"X-Trace-Id": TRACE})
    request_trace = r.headers["x-trace-id"]
    assert request_trace == TRACE

    # 2. The worker runs that job; its handler calls the LLM gateway and the tool executor.
    uow: Any = FakeUow()
    gateway = _gateway()
    executor = ToolExecutor(ToolRegistry(), uow, MagicMock(), MagicMock())
    ctx = BuiltContext(
        messages=[Message(role="user", content="q")], prompt_id="p", prompt_version="1", tokens=5
    )
    tool_ctx = ToolContext(
        workspace_id="ws_abcdef",
        principal_id="usr_1",
        trace_id=request_trace,
        permissions=frozenset(),
    )

    def handler(job: Job, _: JobContext) -> str:
        async def turn() -> None:
            await gateway.structured(LLMTask.COMPOSE, Answer, ctx)
            key = CallKey(execution_id="turn_1", step_id="s1")
            await executor.run("analytics.invented", {}, tool_ctx, key, TurnBudget(5))

        asyncio.run(turn())
        return "done"

    worker = Worker(uow, {JobType.AUDIT_VERIFY.value: handler}, heartbeat_s=60)
    worker._run(_job(request_trace))

    finished = {s.name: s for s in spans.get_finished_spans()}
    assert set(CHAIN) <= finished.keys()
    expected = otel_trace_id(TRACE)
    assert expected == int(TRACE, 16)  # 32-hex ids map 1:1: logs and traces share the id
    for name in CHAIN:
        assert finished[name].context.trace_id == expected, name
    job_span = finished["job.run"].context.span_id
    for child in ("llm.call", "tool.unknown.tool"):
        parent = finished[child].parent
        assert parent is not None
        assert parent.span_id == job_span
    # An LLM-invented tool name never becomes a span name or label.
    assert all("invented" not in s.name for s in spans.get_finished_spans())


def test_non_hex_trace_ids_map_to_a_stable_otel_id() -> None:
    assert otel_trace_id("trace-1234567890") == otel_trace_id("trace-1234567890")
    assert otel_trace_id("trace-1234567890") != otel_trace_id("trace-1234567891")
    assert 0 < otel_trace_id("z" * 32) < 2**128


def test_metrics_use_route_templates_not_ids(client: TestClient) -> None:
    client.get("/api/v1/jobs/job_secret123")  # 401 without a session; still counted
    client.get("/api/v1/no/such/ws_leak999")
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert 'route="/jobs/{job_id}"' in body
    assert 'route="unmatched"' in body
    assert "job_secret123" not in body
    assert "ws_leak999" not in body


def test_metrics_endpoint_can_be_disabled(config: AppConfig) -> None:
    off = config.model_copy(update={"observability": ObservabilityConfig(metrics_enabled=False)})
    with TestClient(create_app(off)) as c:
        assert c.get("/metrics").status_code == 404


def test_otlp_exporter_requires_an_endpoint() -> None:
    with pytest.raises(ValidationError, match="otlp_endpoint"):
        ObservabilityConfig(tracing_exporter="otlp")
