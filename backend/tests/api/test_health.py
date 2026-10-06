from typing import Any
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import __version__
from app.api.health import ReadinessProbe, _llm_ok, get_readiness_probe
from app.core.config import AppConfig, Environment
from app.core.errors import ErrorEnvelope
from app.main import create_app


def test_health_ok(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def _probe(database: bool, job_queue: bool, llm: bool) -> ReadinessProbe:
    return ReadinessProbe(database=lambda: database, job_queue=lambda: job_queue, llm=lambda: llm)


@pytest.mark.parametrize(
    ("flags", "code", "status"),
    [
        ((True, True, True), 200, "ready"),
        ((True, True, False), 200, "degraded"),  # LLM down: serve without AI features
        ((True, False, True), 503, "not_ready"),
        ((False, True, True), 503, "not_ready"),
    ],
)
def test_readiness(
    client: TestClient, flags: tuple[bool, bool, bool], code: int, status: str
) -> None:
    app: Any = client.app
    app.dependency_overrides[get_readiness_probe] = lambda: _probe(*flags)
    r = client.get("/api/v1/health/ready")
    assert r.status_code == code
    body = r.json()
    if code == 503:  # B8: the error envelope; the booleans are its details
        envelope = ErrorEnvelope.model_validate(body)
        assert set(body) == set(ErrorEnvelope.model_fields)
        assert (envelope.error_code, envelope.retryable) == ("NOT_READY", True)
        body = envelope.details or {}
    assert body["status"] == status
    assert set(body) == {"status", "database", "job_queue", "llm"}


def test_readiness_skips_queue_check_when_db_is_down(client: TestClient) -> None:
    app: Any = client.app
    queue = Mock(return_value=True)
    probe = ReadinessProbe(database=lambda: False, job_queue=queue, llm=lambda: True)
    app.dependency_overrides[get_readiness_probe] = lambda: probe
    assert client.get("/api/v1/health/ready").json()["details"]["job_queue"] is False
    queue.assert_not_called()


def test_real_probes_fail_closed_and_leak_nothing(client: TestClient) -> None:
    secret = "password=hunter2 host=db.internal"
    container: Any = Mock()
    container.engine.connect.side_effect = OperationalError("SELECT 1", {}, Exception(secret))
    container.config.llm.endpoint = "http://127.0.0.1:11434"
    app: Any = client.app
    app.dependency_overrides[get_readiness_probe] = lambda: get_readiness_probe(container)
    ollama = MagicMock()
    ollama.__exit__.return_value = False
    ollama.__enter__.return_value.get.side_effect = httpx.ConnectError(secret)
    with patch("app.api.health.httpx.Client", return_value=ollama):
        r = client.get("/api/v1/health/ready")
    assert r.status_code == 503
    flags = {"status": "not_ready", "database": False, "job_queue": False, "llm": False}
    assert r.json()["details"] == flags
    assert "hunter2" not in r.text


def test_llm_probe_requires_the_configured_model(config: AppConfig) -> None:
    def tags(models: list[str]) -> MagicMock:
        resp = Mock()
        resp.json.return_value = {"models": [{"name": m} for m in models]}
        client = MagicMock()
        client.__enter__.return_value.get.return_value = resp
        return client

    container: Any = Mock(config=config)
    with patch("app.api.health.httpx.Client", return_value=tags(["qwen2.5:7b"])):
        assert _llm_ok(container)() is True
    with patch("app.api.health.httpx.Client", return_value=tags(["other:1b"])):
        assert _llm_ok(container)() is False


def test_openapi_served_outside_prod(client: TestClient) -> None:
    assert client.get("/api/openapi.json").status_code == 200


def test_openapi_hidden_in_prod(config: AppConfig) -> None:
    prod = config.model_copy(update={"env": Environment.PROD})
    with TestClient(create_app(prod)) as c:
        assert c.get("/api/openapi.json").status_code == 404
        assert c.get("/api/docs").status_code == 404
