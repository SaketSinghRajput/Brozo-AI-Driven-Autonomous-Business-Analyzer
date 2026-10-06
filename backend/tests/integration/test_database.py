import uuid

import pytest
from alembic import command
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.core.config import DatabaseConfig
from app.core.scope import SystemScope, WorkspaceScope
from app.infra.db import UnitOfWork, make_engine, schema_is_current

from .conftest import TestDatabase, alembic_config

SYSTEM = SystemScope("system:test")
COUNT_WS = text("SELECT count(*) FROM workspaces WHERE id = :id")


def _ws() -> str:
    return f"ws_{uuid.uuid4().hex[:12]}"


def test_migrations_upgrade_downgrade_upgrade(database: TestDatabase) -> None:
    # `unavailable` tool runs (run_plan creates them) are left in place on purpose: 0005's
    # downgrade must handle them (DEBT-007, test_migration_0005_downgrade.py).
    cfg = alembic_config(database.owner_url)
    command.downgrade(cfg, "base")
    assert not schema_is_current(create_engine(database.owner_url))
    command.upgrade(cfg, "head")
    assert schema_is_current(create_engine(database.owner_url))


def test_app_role_cannot_run_ddl(app_engine: Engine) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(text("CREATE TABLE should_fail (id int)"))


def test_app_role_cannot_drop_tables(app_engine: Engine) -> None:
    with pytest.raises(ProgrammingError, match="must be owner"), app_engine.begin() as conn:
        conn.execute(text("DROP TABLE workspaces"))


def test_unit_of_work_commits(app_engine: Engine) -> None:
    uow, ws = UnitOfWork(app_engine), _ws()
    with uow(SYSTEM) as s:
        s.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, 'A')"), {"id": ws})
    with uow(SYSTEM) as s:  # a fresh transaction (unscoped connections see nothing under RLS)
        assert s.execute(COUNT_WS, {"id": ws}).scalar() == 1


def test_unit_of_work_rolls_back_on_error(app_engine: Engine) -> None:
    uow, ws = UnitOfWork(app_engine), _ws()

    def insert_then_fail() -> None:
        with uow(SYSTEM) as s:
            s.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, 'A')"), {"id": ws})
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        insert_then_fail()
    with uow(SYSTEM) as s:
        assert s.execute(COUNT_WS, {"id": ws}).scalar() == 0


def test_workspace_setting_is_transaction_local(app_engine: Engine) -> None:
    uow, ws = UnitOfWork(app_engine), _ws()
    with uow(WorkspaceScope(workspace_id=ws, principal_id="usr_1")) as s:
        assert s.execute(text("SELECT current_setting('app.workspace_id', true)")).scalar() == ws
    with app_engine.connect() as conn:
        value = conn.execute(text("SELECT current_setting('app.workspace_id', true)")).scalar()
        assert value in (None, "")


def test_statement_timeout_is_enforced(database: TestDatabase) -> None:
    engine = make_engine(
        DatabaseConfig(url=SecretStr(database.owner_url), statement_timeout_ms=200)
    )
    with pytest.raises(DBAPIError, match="statement timeout"), engine.connect() as conn:
        conn.execute(text("SELECT pg_sleep(2)"))
    engine.dispose()


def test_app_role_can_check_schema_revision(app_engine: Engine) -> None:
    assert schema_is_current(app_engine)
