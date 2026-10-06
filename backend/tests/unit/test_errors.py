from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.clock import FrozenClock
from app.core.errors import (
    ApplicationError,
    AuthorizationError,
    DatabaseError,
    DataError,
    LLMError,
    NotFoundError,
    install_error_handlers,
)
from app.core.logging import RequestContextMiddleware

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


class Body(BaseModel):
    count: int


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    install_error_handlers(app, FrozenClock(NOW))

    @app.get("/data-error")
    def data_error() -> None:
        raise DataError("The file could not be read as CSV.", details={"hint": "Save as UTF-8."})

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal path C:/srv/db.sqlite")

    @app.post("/body")
    def body(payload: Body) -> Body:
        return payload

    app.add_middleware(RequestContextMiddleware)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize(
    ("exc_type", "status", "code", "retryable"),
    [
        (DataError, 422, "DATA_ERROR", False),
        (NotFoundError, 404, "NOT_FOUND", False),
        (AuthorizationError, 403, "AUTH_ERROR", False),
        (LLMError, 503, "LLM_ERROR", True),
        (DatabaseError, 503, "DATABASE_ERROR", True),
    ],
)
def test_error_classes_carry_http_mapping(
    exc_type: type[ApplicationError], status: int, code: str, retryable: bool
) -> None:
    exc = exc_type("x")
    assert (exc.http_status, exc.code, exc.retryable) == (status, code, retryable)


def test_application_error_renders_envelope(client: TestClient) -> None:
    r = client.get("/data-error")
    assert r.status_code == 422
    body = r.json()
    assert body["error_code"] == "DATA_ERROR"
    assert body["details"] == {"hint": "Save as UTF-8."}
    assert body["retryable"] is False
    assert body["timestamp"].startswith("2026-09-28T10:00:00")
    assert body["trace_id"] == r.headers["x-trace-id"]


def test_unexpected_exception_hides_internals(client: TestClient) -> None:
    r = client.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error_code"] == "SYSTEM_ERROR"
    assert "secret" not in r.text
    assert "C:/srv" not in r.text
    assert "Traceback" not in r.text
    assert body["trace_id"]


def test_request_validation_does_not_echo_input(client: TestClient) -> None:
    r = client.post("/body", json={"count": "not-a-number-SENSITIVE"})
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION_ERROR"
    assert "SENSITIVE" not in r.text


def test_unknown_route_uses_envelope(client: TestClient) -> None:
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.json()["error_code"] == "NOT_FOUND"
