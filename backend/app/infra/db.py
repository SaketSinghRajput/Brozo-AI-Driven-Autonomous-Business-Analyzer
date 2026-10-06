"""Synchronous SQLAlchemy engine and Unit of Work (ADR-0022, design section 51).

The Unit of Work opens one transaction per use: commit on success, rollback on any exception.
For a WorkspaceScope it sets `app.workspace_id`, for a SystemScope `app.system = on`, both with
SET LOCAL semantics (transaction-scoped). PostgreSQL Row-Level Security policies (TASK-DB-012,
migration 0009) key on them as defense in depth behind the repositories' WorkspaceScope checks.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import DatabaseConfig
from app.core.scope import Scope, WorkspaceScope

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def make_engine(cfg: DatabaseConfig) -> Engine:
    """Create the engine lazily (no connection is opened until first use)."""
    return create_engine(
        cfg.url.get_secret_value(),
        pool_size=cfg.pool_size,
        pool_pre_ping=True,
        # Bound parameters (emails, password hashes, tokens) never reach exception text/logs.
        hide_parameters=True,
        connect_args={"options": f"-c statement_timeout={cfg.statement_timeout_ms}"},
    )


class UnitOfWork:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def __call__(self, scope: Scope) -> Iterator[Session]:
        with self._sessions() as session, session.begin():
            # RLS policies (migration 0009) read these; `true` = transaction-local, so nothing
            # leaks to the next user of a pooled connection.
            if isinstance(scope, WorkspaceScope):
                session.execute(
                    text("SELECT set_config('app.workspace_id', :ws, true)"),
                    {"ws": scope.workspace_id},
                )
            else:  # SystemScope: auth, bootstrap, the worker's cross-workspace queue operations
                session.execute(text("SELECT set_config('app.system', 'on', true)"))
            yield session


def schema_is_current(engine: Engine) -> bool:
    """True when the database is at the latest Alembic revision."""
    heads = set(ScriptDirectory(str(MIGRATIONS_DIR)).get_heads())
    with engine.connect() as conn:
        current = set(MigrationContext.configure(conn).get_current_heads())
    return current == heads
