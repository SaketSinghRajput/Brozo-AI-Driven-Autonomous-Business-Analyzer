"""Server-side sessions and the hash-chained audit log.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28

TASK-SEC-004 (ADR-0013): `sessions` stores only the SHA-256 of the opaque session token, so a
database read does not yield usable cookies. The workspace is bound at login and re-checked
against `memberships` on every request.

TASK-DB-009 / TASK-SEC-012 (design §60): `audit_logs` is append-only at the privilege level
(the app role gets SELECT, INSERT only). Each row carries `prev_hash`/`hash`; one chain per
workspace (`workspace_id IS NULL` = the system chain), ordered by `seq`.
Neither table is granted to `analyzer_ro` (the read-only analytics role has no need for them).
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text, primary_key=True),  # sha256 hex of the cookie token
        sa.Column(
            "user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "workspace_id",
            sa.Text,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _ts("created_at"),
        _ts("last_seen_at"),
        _ts("expires_at"),
        _ts("revoked_at", nullable=True),
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires", "sessions", ["expires_at"])

    op.create_table(
        "audit_logs",
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("id", sa.Text, nullable=False, unique=True),
        _ts("at"),
        sa.Column("actor", sa.Text, nullable=False),
        # No FK: audit rows must outlive the workspace they describe.
        sa.Column("workspace_id", sa.Text, nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("resource", sa.Text, nullable=False),
        sa.Column("result", sa.Text, nullable=False),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("prev_hash", sa.Text, nullable=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.CheckConstraint(
            "result IN ('success', 'denied', 'failure')", name="ck_audit_logs_result"
        ),
    )
    op.create_index("ix_audit_logs_chain", "audit_logs", ["workspace_id", "seq"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON sessions TO analyzer_app")
    op.execute("GRANT SELECT, INSERT ON audit_logs TO analyzer_app")  # append-only


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("sessions")
