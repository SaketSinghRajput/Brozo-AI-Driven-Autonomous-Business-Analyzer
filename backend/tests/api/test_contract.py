"""TASK-TEST-005: API contract — no OpenAPI drift, and every error response is the error envelope.

The envelope test walks every registered route and sends it invalid input twice: anonymously
(authN failures) and as a signed-in analyst (bad path ids, malformed JSON / form bodies; with no
database behind the test config, storage errors must also come back as envelopes). Every
response with status >= 400 must be exactly ``ErrorEnvelope`` (same keys, valid types), and no
route may answer invalid input with a 2xx (health probes and /auth/me excepted).
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import current_principal
from app.core.config import AppConfig
from app.core.errors import ErrorEnvelope
from app.core.scope import WorkspaceScope
from app.main import create_app
from tests.principals import principal_for

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "export_openapi.py"
# Probes take no input (readiness failures are envelopes too, B8), and /auth/me has no input to
# make invalid; everything else must reject invalid input.
PROBES = {"/api/v1/health", "/api/v1/health/ready"}
NO_INPUT = {"/api/v1/auth/me"}
FIELDS = set(ErrorEnvelope.model_fields)


def test_openapi_export_has_no_diff() -> None:
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    committed = (SCRIPT.parents[1] / "shared" / "openapi.json").read_text(encoding="utf-8")
    assert module.render() == committed, "shared/openapi.json drifted: run export_openapi.py"


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        own = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return own + [r for v in node.values() for r in _refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


def test_every_openapi_ref_resolves_to_a_component(config: AppConfig) -> None:
    """Client generators cannot follow ``#/$defs/...`` inside a path (GAPS-3d: chat SSE)."""
    doc = create_app(config).openapi()
    components = doc["components"]["schemas"]
    refs = set(_refs(doc["paths"])) | set(_refs(components))
    bad = sorted(r for r in refs if not r.startswith("#/components/schemas/"))
    assert bad == []
    assert {r.removeprefix("#/components/schemas/") for r in refs} <= set(components)
    stream = doc["paths"]["/api/v1/chat/conversations/{conversation_id}/messages"]["post"]
    sse = stream["responses"]["200"]["content"]["text/event-stream"]["schema"]
    assert sse == {"$ref": "#/components/schemas/TurnEvent"}


def _routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every documented operation (included routers are not flat in ``app.routes``)."""
    found = []
    for raw, operations in app.openapi()["paths"].items():
        path = raw.replace("{", "").replace("}", "_INVALID!")
        found += [(method.upper(), path) for method in sorted(operations)]
    return found


def _send(client: TestClient, method: str, path: str) -> Any:
    if method in ("GET", "DELETE"):
        return client.request(method, path, params={"limit": "not-a-number", "cursor": "%%"})
    if path.endswith("/datasets"):  # multipart upload without the file part
        return client.request(method, path, data={"name": ""})
    return client.request(
        method, path, content=b"{not json", headers={"Content-Type": "application/json"}
    )


def _assert_envelope(method: str, path: str, response: Any) -> None:
    body = response.json()
    where = f"{method} {path} -> {response.status_code}: {body}"
    assert set(body) == FIELDS, where
    envelope = ErrorEnvelope.model_validate(body)
    assert envelope.error_code.isupper(), where
    assert envelope.message, where


@pytest.mark.parametrize("signed_in", [False, True], ids=["anonymous", "analyst"])
def test_every_error_response_is_the_envelope(config: AppConfig, signed_in: bool) -> None:
    app = create_app(config)
    if signed_in:
        scope = WorkspaceScope(workspace_id="ws_c0ffee0001", principal_id="usr_contract")
        app.dependency_overrides[current_principal] = lambda: principal_for(scope)
    with TestClient(app, raise_server_exceptions=False) as client:
        routes = _routes(app)
        assert len(routes) >= 20
        for method, path in routes:
            response = _send(client, method, path)
            if path in PROBES and response.status_code < 400:
                continue  # no input to reject; a failing probe (503) is checked below
            if response.status_code < 400:
                assert signed_in, f"{method} {path} accepted an anonymous request"
                assert path in NO_INPUT, f"{method} {path} accepted invalid input"
                continue
            _assert_envelope(method, path, response)
