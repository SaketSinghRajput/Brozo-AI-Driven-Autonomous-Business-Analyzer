"""Dataset version lifecycle columns: state_changed_at, review_flags, ingest_report, dedupe key.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28

TASK-DATA-009/011 (design section 9 "Persistence", ADR-0023). `DatasetVersion` already carries
`state_changed_at` and `review_flags`; 0002 did not create them. `ingest_report` holds the
quarantine sample (reasons only, never row content), column decisions and flags.
Identical bytes are deduplicated per workspace (ADR-0023), so `source_sha256` becomes unique
per workspace (a stronger form of 0002's per-dataset constraint).
NOTE: originally chained after 0005; re-chained after 0006 (auth) at merge time.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dataset_versions",
        sa.Column(
            "state_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "dataset_versions",
        sa.Column("review_flags", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column("dataset_versions", sa.Column("ingest_report", JSONB, nullable=True))
    op.create_unique_constraint(
        "uq_dataset_versions_workspace_source",
        "dataset_versions",
        ["workspace_id", "source_sha256"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_dataset_versions_workspace_source", "dataset_versions", type_="unique")
    for column in ("ingest_report", "review_flags", "state_changed_at"):
        op.drop_column("dataset_versions", column)
