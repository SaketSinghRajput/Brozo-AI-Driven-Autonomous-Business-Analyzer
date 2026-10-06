"""Tracing and metrics (design.md section 61, ADR-0015, TASK-MLOPS-001/002).

Tracer: the OpenTelemetry API. Until `configure_tracing` installs an SDK provider every span is
a no-op (the API's built-in no-op adapter); `console` prints spans, `otlp` needs the optional
`opentelemetry-exporter-otlp-proto-http` package and a local collector.

One trace per request/job: a root span with no active parent takes its OTel trace id from the
application `trace_id` (the X-Trace-Id / job.trace_id context var of app.core.logging), so the
api request, the worker job it enqueued, the LLM calls and the tool calls all share one trace
id, and it equals the trace_id in the JSON logs whenever that id is 32 hex chars.

Metrics: prometheus_client, in-process. Labels are bounded enums only (route *template*, task,
tool name, job type, outcome); never workspace/user/dataset ids or raw paths.
"""

import hashlib
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, TraceFlags
from prometheus_client import Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import ObservabilityConfig
from app.core.logging import trace_id_var

_tracer = trace.get_tracer("app")

HTTP_REQUESTS = Counter("app_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("app_http_request_duration_seconds", "HTTP latency", ["route"])
LLM_CALLS = Counter("app_llm_calls_total", "LLM gateway calls", ["task", "outcome"])
LLM_TOKENS = Counter("app_llm_tokens_total", "LLM tokens", ["task", "kind"])
LLM_LATENCY = Histogram("app_llm_call_duration_seconds", "LLM call latency", ["task"])
TOOL_CALLS = Counter("app_tool_calls_total", "Tool executions", ["tool", "status"])
JOBS = Counter("app_jobs_total", "Jobs finished by the worker", ["type", "outcome"])


def otel_trace_id(app_trace_id: str) -> int:
    """Map an application trace id to a 128-bit OTel trace id (identity for 32-hex ids)."""
    if len(app_trace_id) == 32:
        try:
            value = int(app_trace_id, 16)
        except ValueError:
            value = 0
        if value:
            return value
    return int(hashlib.sha256(app_trace_id.encode()).hexdigest()[:32], 16)


def _parent() -> Context | None:
    if trace.get_current_span().get_span_context().is_valid:
        return None  # nested span: inherit the active trace
    app_trace = trace_id_var.get()
    if not app_trace:
        return None
    remote = SpanContext(
        trace_id=otel_trace_id(app_trace),
        span_id=random.getrandbits(64) or 1,  # an id, not a secret
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(NonRecordingSpan(remote))


@contextmanager
def span(name: str, **attrs: str | int | float | bool) -> Iterator[Span]:
    """Tracer port: `with span("tool.x", tool="x"):`. Attributes must not carry PII."""
    with _tracer.start_as_current_span(name, context=_parent(), attributes=attrs) as s:
        yield s


def configure_tracing(config: ObservabilityConfig) -> None:
    """Install the SDK provider once per process. `none` keeps the no-op API."""
    if config.tracing_exporter == "none":
        return
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SpanExporter,
    )

    exporter: SpanExporter
    if config.tracing_exporter == "console":
        exporter = ConsoleSpanExporter()
    else:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found,unused-ignore]
                OTLPSpanExporter,
            )
        except ImportError as exc:
            raise RuntimeError(
                "tracing_exporter=otlp needs opentelemetry-exporter-otlp-proto-http installed"
            ) from exc
        exporter = OTLPSpanExporter(endpoint=str(config.otlp_endpoint))
    provider = TracerProvider(resource=Resource.create({"service.name": config.service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


class ObservabilityMiddleware:
    """Root `http.request` span + request metrics, labelled by route template (never raw path).

    Must run inside RequestContextMiddleware so the trace id is already set.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500
        started = time.perf_counter()

        async def send_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        method = str(scope.get("method", ""))
        with span("http.request", method=method) as s:
            try:
                await self.app(scope, receive, send_status)
            finally:
                route = route_template(scope)
                s.set_attribute("http.route", route)
                s.set_attribute("http.status_code", status)
                HTTP_REQUESTS.labels(method, route, str(status)).inc()
                HTTP_LATENCY.labels(route).observe(time.perf_counter() - started)


def route_template(scope: Scope) -> str:
    """`/jobs/{job_id}` (template below the /api/v1 mount), never `/jobs/job_123`.

    Unmatched paths collapse into one `unmatched` label (bounded cardinality).
    """
    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"
