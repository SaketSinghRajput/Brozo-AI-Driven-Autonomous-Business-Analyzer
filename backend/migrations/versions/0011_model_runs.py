"""Model runs (append-only).

Revision ID: 0011
Revises: 0012
Create Date: 2026-09-28

TASK-DB-010 (DES-ML-001, TRD-ML-006). One row per ML tool fit (forecast, anomaly, segmentation):
header columns for lookup + the full ``ModelRun`` contract as JSONB. The app role holds only
INSERT/SELECT, so a run record is immutable at the privilege level. ``evidence_id`` has no FK:
the ML tool writes the run before the ToolExecutor writes the evidence (design 19: tools never
write evidence); the link is kept in the evidence arguments/result hash instead. Row-level
security uses the migration 0009 workspace policy (USING + WITH CHECK).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0012"  # chained after conversations at merge; the head is 0011
branch_labels = None
depends_on = None

TASKS = "'forecast', 'anomaly', 'segmentation', 'classification', 'regression'"
# Same condition as migration 0009's workspace tables.
POLICY = (
    "current_setting('app.system', true) = 'on' "
    "OR workspace_id = current_setting('app.workspace_id', true)"
)


def upgrade() -> None:
    op.create_table(
        "model_runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Text,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dataset_version_id", sa.Text, sa.ForeignKey("dataset_versions.id"), nullable=False
        ),
        sa.Column("task", sa.Text, nullable=False),
        sa.Column("model_name", sa.Text, nullable=False),
        sa.Column("model_library", sa.Text, nullable=False),
        sa.Column("seed", sa.BigInteger, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("evidence_id", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"task IN ({TASKS})", name="ck_model_runs_task"),
    )
    op.create_index(
        "ix_model_runs_dataset_version",
        "model_runs",
        ["workspace_id", "dataset_version_id", "created_at"],
    )
    op.execute("GRANT SELECT, INSERT ON model_runs TO analyzer_app")
    op.execute("GRANT SELECT ON model_runs TO analyzer_ro")
    op.execute("ALTER TABLE model_runs ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON model_runs USING ({POLICY}) WITH CHECK ({POLICY})"
    )


def downgrade() -> None:
    op.drop_table("model_runs")  # drops its policy too
