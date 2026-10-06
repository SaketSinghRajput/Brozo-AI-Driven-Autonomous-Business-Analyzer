"""TASK-DATA-013: PostgreSQL snapshot import, against a throw-away source database on the
compose cluster. The API and worker run as `analyzer_app` (grants + RLS apply)."""

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, make_url, text

from app.security.crypto import CredentialCipher

from .dataset_env import Env, make_env
from .scratch import scratch_database

KEY = CredentialCipher.generate_key()
MakeEnv = Callable[..., AbstractContextManager[Env]]


@pytest.fixture
def source() -> Iterator[str]:
    """A source database with `sales` (30 rows) and a function that writes."""
    with scratch_database("connsrc") as url:
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE sales (id int PRIMARY KEY, region text, amount numeric(10,2), "
                    "sold_on date)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO sales SELECT g, CASE WHEN g % 2 = 0 THEN 'north' ELSE 'south' "
                    "END, g * 10.5, DATE '2026-01-01' + g FROM generate_series(1, 30) g"
                )
            )
            conn.execute(
                text(
                    "CREATE FUNCTION bump() RETURNS int LANGUAGE sql AS "
                    "$$ INSERT INTO sales VALUES (1000, 'x', 0, NULL) RETURNING 1 $$"
                )
            )
        engine.dispose()
        yield url


@pytest.fixture
def build(database: Any, app_engine: Engine, tmp_path: Path) -> MakeEnv:
    def _build(**connectors: Any) -> AbstractContextManager[Env]:
        cfg = {"allowed_hosts": ["127.0.0.1", "169.254.169.254"], "allow_loopback": True}
        return make_env(
            database,
            app_engine,
            tmp_path,
            security={"credential_key": KEY},
            connectors={**cfg, **connectors},
        )

    return _build


def body(url: str, **source: str) -> dict[str, Any]:
    parsed = make_url(url)
    return {
        "name": "sales",
        "host": "127.0.0.1",
        "port": parsed.port or 5432,
        "database": parsed.database,
        "username": parsed.username,
        "password": parsed.password,
        "sslmode": "disable",
        **(source or {"table_name": "public.sales"}),
    }


def import_job(env: Env, connector_id: str) -> dict[str, Any]:
    r = env.client.post(f"/api/v1/connectors/{connector_id}/import")
    assert r.status_code == 202, r.text
    env.settle()
    return env.job(r.json()["id"])


def count(url: str) -> int:
    engine = create_engine(url)
    with engine.connect() as conn:
        n = int(conn.execute(text("SELECT count(*) FROM sales")).scalar_one())
    engine.dispose()
    return n


def test_table_import_runs_the_upload_pipeline_and_never_leaks_the_password(
    build: MakeEnv, source: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    password = str(make_url(source).password)
    with build() as env:
        r = env.client.post("/api/v1/connectors", json=body(source))
        assert r.status_code == 201, r.text
        connector = r.json()
        assert "password" not in connector
        assert password not in r.text

        job = import_job(env, connector["id"])
        assert job["status"] == "COMPLETED", job
        listed = env.client.get("/api/v1/connectors")
        assert password not in listed.text
        dataset_id = listed.json()[0]["dataset_id"]
        assert dataset_id
        version = env.version({"dataset_id": dataset_id, "version_id": job["result_ref"]})
        assert version["row_count"] == 30
        assert version["state"] == "READY"  # the full upload chain ran, through run_plan

        writer = create_engine(source)
        with writer.begin() as conn:  # a later import becomes a new version
            conn.execute(text("INSERT INTO sales VALUES (31, 'north', 1, DATE '2026-03-01')"))
        writer.dispose()
        again = import_job(env, connector["id"])
        assert again["status"] == "COMPLETED", again
        versions = env.get(f"/{dataset_id}/versions").json()
        assert [v["row_count"] for v in versions] == [30, 31]

        with env.container.uow(env.scope) as s:
            sealed = s.execute(text("SELECT password_enc FROM connectors")).scalar_one()
            actions = s.execute(
                text("SELECT action, result, metadata::text AS meta FROM audit_logs ORDER BY seq")
            ).all()
    assert sealed.startswith("v1.")
    assert password not in sealed
    assert [a.action for a in actions].count("connector.import") == 2
    assert ("connector.create", "success") in [(a.action, a.result) for a in actions]
    assert all(password not in a.meta for a in actions)
    assert password not in caplog.text


def test_a_query_that_writes_fails_read_only(build: MakeEnv, source: str) -> None:
    with build() as env:
        created = env.client.post("/api/v1/connectors", json=body(source, query="SELECT bump()"))
        assert created.status_code == 201, created.text
        job = import_job(env, created.json()["id"])
    assert job["status"] == "FAILED"
    assert job["error"]["message"].startswith("[CONNECTOR_READ_ONLY_VIOLATION]")
    assert count(source) == 30


def test_statement_timeout_cancels_the_source_query(build: MakeEnv, source: str) -> None:
    with build(statement_timeout_ms=300) as env:
        query = "SELECT pg_sleep(5) AS slept"
        created = env.client.post("/api/v1/connectors", json=body(source, query=query))
        job = import_job(env, created.json()["id"])
    assert job["status"] == "FAILED"
    assert job["error"]["message"].startswith("[CONNECTOR_TIMEOUT]")


def test_row_cap_fails_instead_of_truncating(build: MakeEnv, source: str) -> None:
    with build(max_rows=10) as env:
        created = env.client.post("/api/v1/connectors", json=body(source))
        job = import_job(env, created.json()["id"])
        assert env.get("").json() == []  # no dataset was registered
    assert job["status"] == "FAILED"
    assert job["error"]["message"].startswith("[ROW_CAP_EXCEEDED]")


@pytest.mark.parametrize(
    ("host", "reason"),
    [
        ("169.254.169.254", "CONNECTOR_ADDRESS_REFUSED"),  # allow-listed, still refused
        ("db.internal.example", "CONNECTOR_HOST_NOT_ALLOWED"),
    ],
)
def test_ssrf_targets_are_refused_and_audited(
    build: MakeEnv, source: str, host: str, reason: str
) -> None:
    with build() as env:
        r = env.client.post("/api/v1/connectors", json={**body(source), "host": host})
        assert r.status_code == 400, r.text
        assert r.json()["details"]["reason"] == reason
        with env.container.uow(env.scope) as s:
            assert s.execute(text("SELECT count(*) FROM connectors")).scalar_one() == 0
            denied = s.execute(
                text("SELECT metadata->>'reason' FROM audit_logs WHERE result = 'denied'")
            ).scalars()
            assert list(denied) == [reason]


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM sales",
        "SELECT 1; DROP TABLE sales",
        "WITH gone AS (DELETE FROM sales RETURNING *) SELECT * FROM gone",
        "SELECT 1 -- comment",
        "SELECT /* x */ 1",
        "COPY sales TO PROGRAM 'id'",
    ],
)
def test_non_select_queries_are_rejected(build: MakeEnv, source: str, query: str) -> None:
    with build() as env:
        r = env.client.post("/api/v1/connectors", json=body(source, query=query))
        assert r.status_code == 422, r.text
        assert r.json()["details"]["reason"] == "CONNECTOR_QUERY_REJECTED"
    assert count(source) == 30


def test_bad_table_name_is_rejected(build: MakeEnv, source: str) -> None:
    with build() as env:
        r = env.client.post("/api/v1/connectors", json=body(source, table_name='sales"; --'))
    assert r.status_code == 422
    assert r.json()["details"]["reason"] == "CONNECTOR_BAD_TABLE"


def test_connectors_are_disabled_without_a_credential_key(
    database: Any, app_engine: Engine, tmp_path: Path, source: str
) -> None:
    with make_env(database, app_engine, tmp_path) as env:
        r = env.client.post("/api/v1/connectors", json=body(source))
    assert r.status_code == 503
    assert r.json()["error_code"] == "CONNECTORS_DISABLED"
