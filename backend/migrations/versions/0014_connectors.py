"""External database connectors (PostgreSQL snapshot import).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-04

TASK-DATA-013. One row per configured source: where to connect (host/port/database/user,
sslmode) and what to import (exactly one of a table name or a single SELECT). The password is
stored only as `password_enc`, AES-256-GCM sealed by `CredentialCipher` with the context
`connector:<id>` as associated data; the key never lives in the database. `dataset_id` is set
by the first import so later imports become new versions of the same dataset.

The app role may SELECT/INSERT and UPDATE (dataset_id); `analyzer_ro` gets nothing (the table
holds sealed credentials). Workspace RLS policy as in migration 0009 (ADR-0013).
Chained after 0013 (analysis plans/runs) at the merge of feature/p1-mvp.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

POLICY = (
    "current_setting('app.system', true) = 'on' "
    "OR workspace_id = current_setting('app.workspace_id', true)"
)
SSLMODES = "'disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'"


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Text,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("host", sa.Text, nullable=False),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("database", sa.Text, nullable=False),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("sslmode", sa.Text, nullable=False),
        sa.Column("source_table", sa.Text, nullable=True),
        sa.Column("source_query", sa.Text, nullable=True),
        sa.Column("password_enc", sa.Text, nullable=False),
        sa.Column(
            "dataset_id", sa.Text, sa.ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('postgres')", name="ck_connectors_kind"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="ck_connectors_port"),
        sa.CheckConstraint(f"sslmode IN ({SSLMODES})", name="ck_connectors_sslmode"),
        sa.CheckConstraint(
            "(source_table IS NULL) <> (source_query IS NULL)", name="ck_connectors_one_source"
        ),
    )
    op.create_index("ix_connectors_workspace", "connectors", ["workspace_id", "created_at"])
    op.execute("GRANT SELECT, INSERT, UPDATE ON connectors TO analyzer_app")
    op.execute("ALTER TABLE connectors ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON connectors USING ({POLICY}) WITH CHECK ({POLICY})"
    )


def downgrade() -> None:
    op.drop_table("connectors")
