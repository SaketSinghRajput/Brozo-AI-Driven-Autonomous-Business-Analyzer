"""Security hardening: keyed audit chain, auth tables hidden from analyzer_ro, RLS.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-28

L4: `audit_logs.key_id` names the HMAC key each row was written with (rotation). Rows written
before this revision were plain SHA-256 links; they get key id `legacy-sha256`, which no keyring
contains, so a verifier reports those chains as broken at their first row. There is no
production data yet (P1); a dev database with old rows should be re-created.

L3: the read-only analytics role `analyzer_ro` loses SELECT on `users` (emails, password
hashes) and `memberships` (who holds which role), granted by 0001. `sessions` and `audit_logs`
were never granted to it (0006).

TASK-DB-012: Row-Level Security as defense in depth behind the repositories' WorkspaceScope
filters. The UnitOfWork sets, per transaction (`set_config(..., true)`):
- `app.workspace_id` for a WorkspaceScope: rows of that workspace only, for reads *and* writes
  (WITH CHECK), so a query that forgets its WHERE clause cannot cross workspaces;
- `app.system = 'on'` for a SystemScope: all rows. SystemScope is a capability object held only
  by system components (login/session checks, bootstrap, the worker's queue claim/heartbeat/reap,
  which must see every workspace's jobs) and is never built from request input.
A connection with neither setting sees no rows. `users` has no workspace: SystemScope only.
`workspaces` is keyed by `id`. `outbox_events` has no workspace column and stays without RLS
(event payloads carry ids only). The tables are owned by the migration role, not by
`analyzer_app`, so RLS applies to the app without FORCE ROW LEVEL SECURITY; superusers and the
owner bypass it (migrations, the test harness's tamper checks).
"""

from alembic import op

SYSTEM = "current_setting('app.system', true) = 'on'"
WORKSPACE = "current_setting('app.workspace_id', true)"
WORKSPACE_TABLES = (
    "datasets",
    "dataset_versions",
    "dataset_profiles",
    "understandings",
    "semantic_models",
    "jobs",
    "evidence",
    "tool_runs",
    "claims",
    "validation_results",
    "memberships",
    "sessions",
    "audit_logs",
)
POLICIES = {
    **{t: f"{SYSTEM} OR workspace_id = {WORKSPACE}" for t in WORKSPACE_TABLES},
    "workspaces": f"{SYSTEM} OR id = {WORKSPACE}",
    "users": SYSTEM,
}

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_logs ADD COLUMN key_id text NOT NULL DEFAULT 'legacy-sha256'")
    op.execute("ALTER TABLE audit_logs ALTER COLUMN key_id DROP DEFAULT")
    op.execute("REVOKE ALL ON users, memberships FROM analyzer_ro")
    for table, condition in POLICIES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} "
            f"USING ({condition}) WITH CHECK ({condition})"
        )


def downgrade() -> None:
    for table in POLICIES:
        op.execute(f"DROP POLICY workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT ON users, memberships TO analyzer_ro")
    op.execute("ALTER TABLE audit_logs DROP COLUMN key_id")
