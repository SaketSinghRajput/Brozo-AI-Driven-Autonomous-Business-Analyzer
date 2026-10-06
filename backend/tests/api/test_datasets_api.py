"""TASK-DATA-010/012 API checks that need no database (all rejected before any DB access)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import current_principal
from app.core.config import AppConfig, Environment
from app.core.scope import WorkspaceScope
from app.main import create_app
from tests.principals import principal_for

LIMIT = 100 * 1024


@pytest.fixture
def scoped(tmp_path: Path) -> Iterator[TestClient]:
    config = AppConfig(
        _env_file=None,
        env=Environment.TEST,
        database={
            "url": "postgresql+psycopg://test:test@127.0.0.1:5432/test",
            "check_schema_on_startup": False,
        },
        storage={"datastore_path": tmp_path / "store", "max_upload_bytes": LIMIT},
        security={"session_secret": "t" * 32},
    )
    app = create_app(config)
    app.dependency_overrides[current_principal] = lambda: principal_for(
        WorkspaceScope("ws_abcdef", "usr_test")
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _stored_files(tmp_path: Path) -> list[Path]:
    return [p for p in (tmp_path / "store").rglob("*") if p.is_file()]


def test_dataset_endpoints_fail_closed_without_authentication(client: TestClient) -> None:
    for path in (
        "/api/v1/datasets",
        "/api/v1/datasets/ds_1",
        "/api/v1/datasets/ds_1/versions",
        "/api/v1/datasets/ds_1/versions/dsv_1/profile",
        "/api/v1/datasets/ds_1/versions/dsv_1/quarantine",
    ):
        assert client.get(path).status_code == 401, path
    r = client.post("/api/v1/datasets", files={"file": ("a.csv", b"a\n1\n")})
    assert r.status_code == 401


def test_content_length_precheck_rejects_before_parsing(scoped: TestClient, tmp_path: Path) -> None:
    r = scoped.post("/api/v1/datasets", files={"file": ("big.csv", b"a,b\n" + b"1,2\n" * LIMIT)})
    assert r.status_code == 413
    body = r.json()
    assert body["error_code"] == "SECURITY_ERROR"
    assert body["details"]["reason"] == "OVERSIZED_FILE"
    assert _stored_files(tmp_path) == []


@pytest.mark.parametrize(
    ("content", "status", "reason"),
    [(b"%PDF-1.7 ...", 415, "UNSUPPORTED_FORMAT"), (b"", 422, "EMPTY_FILE")],
)
def test_upload_rejections_leave_nothing_stored(
    scoped: TestClient, tmp_path: Path, content: bytes, status: int, reason: str
) -> None:
    r = scoped.post("/api/v1/datasets", files={"file": ("x.csv", content)})
    assert r.status_code == status
    assert r.json()["details"]["reason"] == reason
    assert _stored_files(tmp_path) == []


def test_missing_file_field_is_a_validation_error(scoped: TestClient) -> None:
    r = scoped.post("/api/v1/datasets", data={"name": "x"})
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION_ERROR"


def test_upload_is_documented_as_multipart(scoped: TestClient) -> None:
    spec = scoped.get("/api/openapi.json").json()
    body = spec["paths"]["/api/v1/datasets"]["post"]["requestBody"]["content"]
    assert body["multipart/form-data"]["schema"]["required"] == ["file"]
