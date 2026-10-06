"""Validation results (append-only) and the claim -> validation link.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28

TASK-DB-006 (DES-VAL-002) and TASK-EVID-004. Chained after 0007 (dataset lifecycle) at merge;
the validation stream originally targeted 0005.

- ``validation_results``: header columns + the full ``ValidationResult`` as JSONB. The app role
  holds only INSERT/SELECT, so results are immutable at the privilege level. Subjects (answers,
  widgets) have no table yet, so there is no FK to the subject.
- ``claims.validation_id``: the ``Claim`` contract carries it (design 39), migration 0004 did not.
- ``claims.text_span`` becomes nullable: widget claims have a widget_id and no text.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

STATUSES = "'VALID', 'INVALID', 'UNCERTAIN', 'REQUIRES_REVIEW'"
SUBJECTS = "'answer', 'widget', 'tool_result', 'insight'"


def upgrade() -> None:
    op.create_table(
        "validation_results",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Text,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.Text, nullable=False),
        sa.Column("subject_id", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_validation_results_status"),
        sa.CheckConstraint(f"subject_type IN ({SUBJECTS})", name="ck_validation_results_subject"),
    )
    op.create_index(
        "ix_validation_results_subject",
        "validation_results",
        ["workspace_id", "subject_type", "subject_id", "created_at"],
    )
    op.add_column(
        "claims",
        sa.Column("validation_id", sa.Text, sa.ForeignKey("validation_results.id"), nullable=True),
    )
    op.alter_column("claims", "text_span", nullable=True)
    op.execute("GRANT SELECT, INSERT ON validation_results TO analyzer_app")
    op.execute("GRANT SELECT ON validation_results TO analyzer_ro")


def downgrade() -> None:
    op.execute("UPDATE claims SET text_span = '' WHERE text_span IS NULL")
    op.alter_column("claims", "text_span", nullable=False)
    op.drop_column("claims", "validation_id")
    op.drop_table("validation_results")
