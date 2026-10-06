"""DEBT-007: migration 0005's downgrade must succeed with an `unavailable` tool run present."""

from alembic import command
from sqlalchemy import create_engine, text

from .conftest import alembic_config
from .scratch import scratch_database


def test_downgrade_maps_unavailable_runs_then_upgrades_again() -> None:
    with scratch_database("mig0005") as url:
        engine = create_engine(url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                conn.execute(text("GRANT USAGE ON SCHEMA public TO analyzer_app, analyzer_ro"))
            cfg = alembic_config(url)
            command.upgrade(cfg, "0005")
            with engine.connect() as conn:
                conn.execute(text("INSERT INTO workspaces (id, name) VALUES ('ws_m5', 'm5')"))
                conn.execute(
                    text(
                        "INSERT INTO tool_runs (id, workspace_id, tool, tool_version, args, "
                        "status, duration_ms) VALUES "
                        "('tr_u', 'ws_m5', 't', '1', '{}', 'unavailable', 1), "
                        "('tr_e', 'ws_m5', 't', '1', '{}', 'error', 1)"
                    )
                )
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "0004")
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id, status, coalesce(error_code, '') FROM tool_runs ORDER BY id")
                ).all()
            assert [tuple(r) for r in rows] == [
                ("tr_e", "error", ""),
                ("tr_u", "error", "UNAVAILABLE"),
            ]
            command.upgrade(cfg, "head")
        finally:
            engine.dispose()
