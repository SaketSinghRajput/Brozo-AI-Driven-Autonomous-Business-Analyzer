"""Structured JSON logging with per-request trace IDs (design.md section 61)."""

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

TRACE_HEADER = "x-trace-id"
_VALID_TRACE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_SENSITIVE = re.compile(
    r"pass|secret|token|authorization|cookie|api[_-]?key|credential|e-?mail", re.I
)
REDACTED = "[REDACTED]"


def _redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Structured fields go in `extra={"fields": {...}}`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "trace_id": trace_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(_redact(fields))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


class RequestContextMiddleware:
    """Pure ASGI middleware: assigns a trace ID, echoes it in X-Trace-Id, logs one line/request.

    Logs method, path (no query string: it may carry PII), status and duration only.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.log = logging.getLogger("app.request")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(TRACE_HEADER.encode(), b"").decode()
        trace_id = incoming if _VALID_TRACE.match(incoming) else uuid.uuid4().hex
        trace_id_var.set(trace_id)
        started = time.perf_counter()
        status = 500

        async def send_with_trace(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                # Replace (not append): error handlers may already have set the header.
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != TRACE_HEADER.encode()
                ]
                headers.append((TRACE_HEADER.encode(), trace_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            self.log.info(
                "http.request",
                extra={
                    "fields": {
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                },
            )
