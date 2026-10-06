import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.core.logging import REDACTED, JsonFormatter, trace_id_var


def _format(fields: dict[str, object]) -> dict[str, object]:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "evt", None, None)
    record.fields = fields
    result: dict[str, object] = json.loads(JsonFormatter().format(record))
    return result


def test_json_line_has_core_fields() -> None:
    trace_id_var.set("abc12345")
    out = _format({"rows": 10})
    assert out["event"] == "evt"
    assert out["level"] == "INFO"
    assert out["trace_id"] == "abc12345"
    assert out["rows"] == 10


def test_sensitive_keys_are_redacted_recursively() -> None:
    out = _format(
        {"password": "p", "db": {"api_key": "k", "host": "h"}, "Authorization": "Bearer x"}
    )
    assert out["password"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["db"] == {"api_key": REDACTED, "host": "h"}


def test_request_gets_trace_id_header(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert len(r.headers["x-trace-id"]) >= 8


def test_valid_incoming_trace_id_is_propagated(client: TestClient) -> None:
    r = client.get("/api/v1/health", headers={"X-Trace-Id": "trace-1234567890"})
    assert r.headers["x-trace-id"] == "trace-1234567890"


@pytest.mark.parametrize("bad", ["short", "has space in it!!", "x" * 65, "semi;colon;value"])
def test_invalid_incoming_trace_id_is_replaced(client: TestClient, bad: str) -> None:
    r = client.get("/api/v1/health", headers={"X-Trace-Id": bad})
    assert r.headers["x-trace-id"] != bad
