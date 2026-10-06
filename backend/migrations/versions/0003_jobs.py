"""Job queue and transactional outbox.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28

TASK-DB-004 (ADR-0010, design §50-53, TRD §31.2).
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STATUSES = "'QUEUED', 'RUNNING', 'RETRYING', 'COMPLETED', 'FAILED', 'CANCELLED'"


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=default)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Text,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.Text, nullable=False, server_default="QUEUED"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        _ts("run_after"),
        sa.Column("locked_by", sa.Text, nullable=True),
        _ts("locked_at", nullable=True),
        _ts("heartbeat_at", nullable=True),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("progress", sa.Float, nullable=False, server_default="0"),
        sa.Column("result_ref", sa.Text, nullable=True),
        sa.Column("error", JSONB, nullable=True),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        sa.Column("trace_id", sa.Text, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_jobs_status"),
        sa.CheckConstraint("progress BETWEEN 0 AND 1", name="ck_jobs_progress"),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_jobs_attempts"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_jobs_idempotency"),
    )
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["run_after"],
        postgresql_where=sa.text("status IN ('QUEUED', 'RETRYING')"),
    )
    op.create_index(
        "ix_jobs_running_heartbeat",
        "jobs",
        ["heartbeat_at"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_index("ix_jobs_workspace_created", "jobs", ["workspace_id", "created_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("payload", JSONB, nullable=False),
        _ts("created_at"),
        _ts("dispatched_at", nullable=True),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("dispatched_at IS NULL"),
    )

    op.execute("GRANT SELECT, INSERT, UPDATE ON jobs, outbox_events TO analyzer_app")
    op.execute("GRANT SELECT ON jobs, outbox_events TO analyzer_ro")


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("jobs")
