"""Ephemeral PostgreSQL harness (TASK-TEST-006).

Set APP_TEST_ADMIN_DATABASE_URL to a superuser/owner URL on a disposable cluster, e.g. the
compose postgres: postgresql+psycopg://analyzer:<pw>@127.0.0.1:5432/postgres
Each test session creates a uniquely named database, migrates it to head, and drops it at the end.
Integration tests are skipped (not failed) when the variable is not set.
"""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, make_url, text

BACKEND = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get("APP_TEST_ADMIN_DATABASE_URL")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.path):
            item.add_marker(pytest.mark.integration)
            if not ADMIN_URL:
                item.add_marker(pytest.mark.skip(reason="APP_TEST_ADMIN_DATABASE_URL not set"))


@dataclass(frozen=True)
class TestDatabase:
    owner_url: str


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _ensure_roles(admin: Engine) -> None:
    with admin.connect() as conn:
        if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname='analyzer_app'")).first():
            conn.execute(text("CREATE ROLE analyzer_app NOLOGIN"))
        if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname='analyzer_ro'")).first():
            conn.execute(text("CREATE ROLE analyzer_ro NOLOGIN"))


@pytest.fixture(scope="session")
def empty_database() -> Iterator[TestDatabase]:
    assert ADMIN_URL
    base = make_url(ADMIN_URL)
    name = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    _ensure_roles(admin)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    owner_url = base.set(database=name)
    owner = create_engine(owner_url, isolation_level="AUTOCOMMIT")
    with owner.connect() as conn:
        conn.execute(text("GRANT USAGE ON SCHEMA public TO analyzer_app, analyzer_ro"))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO analyzer_app'))
    owner.dispose()
    yield TestDatabase(owner_url=owner_url.render_as_string(hide_password=False))
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="session")
def database(empty_database: TestDatabase) -> TestDatabase:
    command.upgrade(alembic_config(empty_database.owner_url), "head")
    return empty_database


@pytest.fixture
def app_engine(database: TestDatabase) -> Iterator[Engine]:
    """Engine whose connections act as `analyzer_app` (least privilege) via SET ROLE.

    The owner connects and switches role, so the harness never needs the app role's password.
    """
    engine = create_engine(database.owner_url)

    @event.listens_for(engine, "connect")
    def _as_app_role(dbapi_conn: Any, _: Any) -> None:
        dbapi_conn.autocommit = True
        dbapi_conn.execute("SET ROLE analyzer_app")
        dbapi_conn.autocommit = False

    yield engine
    engine.dispose()


from .dataset_env import env  # noqa: E402, F401  (shared fixture for the dataset pipeline tests)
