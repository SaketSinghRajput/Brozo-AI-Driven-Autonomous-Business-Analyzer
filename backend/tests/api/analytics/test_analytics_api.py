"""TASK-ANALYSIS-016 without a database: the endpoint is scoped (fails closed) and validates its
body before any work. The evidence-producing flow is in tests/integration/analytics."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import current_principal
from app.core.config import AppConfig
from app.core.scope import WorkspaceScope
from app.main import create_app
from tests.principals import principal_for

URL = "/api/v1/analytics/query"
BODY = {"dataset_version_id": "dsv_x1", "semantic_model_version": 1, "request": {"metric": "R"}}
EMPTY_IN = [{"dimension": "d", "op": "in", "value": []}]


def test_query_fails_closed_without_authentication(client: TestClient) -> None:
    r = client.post(URL, json=BODY)
    assert (r.status_code, r.json()["error_code"]) == (401, "AUTH_ERROR")


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "dataset_version_id": "x; DROP TABLE evidence"},
        {**BODY, "semantic_model_version": 0},
        {**BODY, "request": {"metric": "R", "unexpected": 1}},
        {**BODY, "request": {"metric": "R", "limit": 10_001}},
        {**BODY, "request": {"metric": "R", "compare_to": "previous_period"}},  # needs a range
        {**BODY, "request": {"metric": "R", "filters": EMPTY_IN}},
        {**BODY, "extra": True},
    ],
)
def test_invalid_bodies_are_rejected(config: AppConfig, body: dict[str, Any]) -> None:
    app = create_app(config)
    app.dependency_overrides[current_principal] = lambda: principal_for(
        WorkspaceScope(workspace_id="ws_abcdef", principal_id="usr_1")
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post(URL, json=body)
    assert (r.status_code, r.json()["error_code"]) == (422, "VALIDATION_ERROR")
