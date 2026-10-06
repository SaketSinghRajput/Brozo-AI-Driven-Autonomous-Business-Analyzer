#!/usr/bin/env python
"""Migration drift check (TASK-DB-013, design section 81).

Migrations are hand-written SQL (alembic env has no target_metadata), so `alembic check` has
nothing to compare against. Instead this compares catalog snapshots:

1. exactly one Alembic head;
2. a scratch database migrated base -> head equals itself after head -> base -> head
   (every downgrade really undoes its upgrade);
3. with --live-url: the live database's schema equals the freshly migrated one
   (no manual schema edits in staging/production).

Run (from backend/, so the backend environment is used):
    uv run python ../scripts/check_schema_drift.py --admin-url postgresql+psycopg://...
    uv run python ../scripts/check_schema_drift.py --admin-url ... --live-url ...
--admin-url must be a superuser/owner URL on a disposable cluster (it creates and drops a
scratch database). Exit code 1 on drift, with a diff of the differing catalog rows.
"""

import argparse
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, make_url, text

BACKEND = Path(__file__).resolve().parents[1] / "backend"

# name -> query returning comparable rows for the public schema.
CATALOG = {
    "columns": """
        SELECT table_name, column_name, data_type, udt_name, is_nullable,
               coalesce(column_default, ''), coalesce(character_maximum_length, 0)
        FROM information_schema.columns WHERE table_schema = 'public'""",
    "constraints": """
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
        FROM pg_constraint WHERE connamespace = 'public'::regnamespace""",
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
    "policies": """
        SELECT tablename, policyname, cmd, roles::text, coalesce(qual, ''),
               coalesce(with_check, '')
        FROM pg_policies WHERE schemaname = 'public'""",
    "rls": """
        SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
        WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p')""",
    "grants": """
        SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants
        WHERE table_schema = 'public' AND grantee IN ('analyzer_app', 'analyzer_ro')""",
    "triggers": """
        SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) FROM pg_trigger
        WHERE NOT tgisinternal""",
    "functions": """
        SELECT p.proname, pg_get_function_identity_arguments(p.oid), md5(p.prosrc)
        FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace""",
}

Snapshot = dict[str, set[tuple[str, ...]]]


def snapshot(url: str) -> Snapshot:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                name: {tuple(str(v) for v in row) for row in conn.execute(text(sql))}
                for name, sql in CATALOG.items()
            }
    finally:
        engine.dispose()


def diff(expected: Snapshot, actual: Snapshot, label: str) -> list[str]:
    out = []
    for name in CATALOG:
        out += [f"  {name}: only in migrations: {r}" for r in sorted(expected[name] - actual[name])]
        out += [f"  {name}: only in {label}: {r}" for r in sorted(actual[name] - expected[name])]
    return out


def live_revision(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@contextmanager
def scratch_database(admin_url: str) -> Iterator[str]:
    base = make_url(admin_url)
    name = f"drift_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        for role in ("analyzer_app", "analyzer_ro"):  # created by initdb in compose
            exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
            if not exists.first():
                conn.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    owner = create_engine(url, isolation_level="AUTOCOMMIT")
    with owner.connect() as conn:
        conn.execute(text("GRANT USAGE ON SCHEMA public TO analyzer_app, analyzer_ro"))
    owner.dispose()
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration drift check (TASK-DB-013)")
    parser.add_argument("--admin-url", required=True, help="superuser URL, disposable cluster")
    parser.add_argument("--live-url", help="database whose schema must match the migrations")
    args = parser.parse_args()

    heads = ScriptDirectory.from_config(alembic_config("postgresql://unused")).get_heads()
    if len(heads) != 1:
        print(f"FAIL: expected one Alembic head, found {heads}")
        return 1
    problems: list[str] = []
    with scratch_database(args.admin_url) as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        migrated = snapshot(url)
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        problems += diff(migrated, snapshot(url), "re-upgrade after downgrade")
        if args.live_url:
            live_rev = live_revision(args.live_url)
            if live_rev != heads[0]:
                # Behind is not drift: compare against the migrations at the live revision.
                print(f"WARN: live database at revision {live_rev}, head is {heads[0]}")
                command.downgrade(cfg, live_rev or "base")
            problems += diff(snapshot(url), snapshot(args.live_url), "live database")
    if problems:
        print("FAIL: schema drift\n" + "\n".join(problems))
        return 1
    tables = len({row[0] for row in migrated["columns"]})
    checked = "round-trip" + (" + live" if args.live_url else "")
    print(f"OK: head {heads[0]}, {tables} tables, {checked} identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
