"""Identity schema: workspaces, users, memberships.

Revision ID: 0001
Revises:
Create Date: 2026-09-28

Privileges: the application role `analyzer_app` gets DML only (no DDL: it does not own the
schema). `analyzer_ro` gets SELECT. Both roles are created by infra/compose/initdb.
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = ("workspaces", "users", "memberships")


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        _created_at(),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        _created_at(),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
    )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_table(
        "memberships",
        sa.Column(
            "user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "workspace_id",
            sa.Text,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("user_id", "workspace_id"),
        sa.CheckConstraint("role IN ('viewer', 'analyst', 'admin')", name="ck_memberships_role"),
    )
    op.create_index("ix_memberships_workspace", "memberships", ["workspace_id"])
    tables = ", ".join(TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO analyzer_app")
    op.execute(f"GRANT SELECT ON {tables} TO analyzer_ro")
    # The app checks the schema revision at startup (TASK-DB-002) and must read it.
    op.execute("GRANT SELECT ON alembic_version TO analyzer_app, analyzer_ro")


def downgrade() -> None:
    op.drop_table("memberships")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("workspaces")
