"""Tool runs, evidence and claims (append-only).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28

TASK-DB-005 (DES-EVID-001, TRD-EVID-001). The app role may only INSERT and SELECT these
tables: evidence is immutable at the privilege level, not just by convention.
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TABLES = "tool_runs, evidence, claims"


def _ws() -> sa.Column[Any]:
    return sa.Column(
        "workspace_id", sa.Text, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.Text, primary_key=True),  # deterministic: (execution, step, attempt)
        _ws(),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("dataset_version_id", sa.Text, sa.ForeignKey("dataset_versions.id")),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("result_hash", sa.Text, nullable=False),
        _created_at(),
    )
    op.create_index("ix_evidence_workspace_created", "evidence", ["workspace_id", "created_at"])
    op.create_index("ix_evidence_dataset_version", "evidence", ["dataset_version_id"])

    op.create_table(
        "tool_runs",
        sa.Column("id", sa.Text, primary_key=True),
        _ws(),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("tool_version", sa.Text, nullable=False),
        sa.Column("args", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("evidence_id", sa.Text, sa.ForeignKey("evidence.id"), nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        _created_at(),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_tool_runs_status"),
    )
    op.create_index("ix_tool_runs_workspace_created", "tool_runs", ["workspace_id", "created_at"])
    op.create_index("ix_tool_runs_evidence", "tool_runs", ["evidence_id"])

    op.create_table(
        "claims",
        sa.Column("id", sa.Text, primary_key=True),
        _ws(),
        sa.Column("answer_id", sa.Text, nullable=True),
        sa.Column("widget_id", sa.Text, nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("text_span", sa.Text, nullable=False),
        sa.Column("evidence_ids", ARRAY(sa.Text), nullable=False),
        _created_at(),
        sa.CheckConstraint("cardinality(evidence_ids) >= 1", name="ck_claims_has_evidence"),
    )
    op.create_index("ix_claims_workspace_created", "claims", ["workspace_id", "created_at"])

    op.execute(f"GRANT SELECT, INSERT ON {TABLES} TO analyzer_app")
    op.execute(f"GRANT SELECT ON {TABLES} TO analyzer_ro")


def downgrade() -> None:
    op.drop_table("claims")
    op.drop_table("tool_runs")
    op.drop_table("evidence")
