"""Analysis plans and runs (step states + generated dashboard).

Revision ID: 0013
Revises: 0011
Create Date: 2026-09-28

TASK-DB-007 (design §18, §28). Chained after 0011 (model_runs, ML stream).

- ``analysis_plans``: the validated plan a run executed. Insert-only; the id is deterministic in
  (dataset version, semantic model version, rules version, config version), so re-planning the
  same inputs is a no-op.
- ``analysis_runs``: one execution of a plan. ``steps`` holds the per-step state map
  (PENDING / RUNNING / SUCCEEDED / FAILED / SKIPPED / UNAVAILABLE / CANCELLED with attempts,
  evidence id and error), updated after every transition so progress survives a worker restart.
  ``dashboard`` is the DashboardSpec generated from the run (NULL until generated).
- ``evidence.result_rows``: a bounded preview (<= 500 rows, typed columns) of the tabular result
  a tool call returned, written in the same INSERT as the evidence row (the app role still holds
  only INSERT/SELECT on ``evidence``, so it stays immutable). Chart widgets read it through
  ``GET /api/v1/evidence/{id}/data``; the Evidence contract itself is unchanged.
Both new tables get the migration-0009 ``workspace_isolation`` RLS policy (ADR-0013).
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0011"
branch_labels = None
depends_on = None

POLICY = (
    "current_setting('app.system', true) = 'on' "
    "OR workspace_id = current_setting('app.workspace_id', true)"
)
RUN_STATES = "'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED'"


def _fk(name: str, target: str) -> sa.Column[Any]:
    return sa.Column(name, sa.Text, sa.ForeignKey(target, ondelete="CASCADE"), nullable=False)


def _at(name: str, nullable: bool = False) -> sa.Column[Any]:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "analysis_plans",
        sa.Column("id", sa.Text, primary_key=True),
        _fk("workspace_id", "workspaces.id"),
        _fk("dataset_version_id", "dataset_versions.id"),
        sa.Column("semantic_model_version", sa.Integer, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        _at("created_at"),
    )
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Text, primary_key=True),
        _fk("workspace_id", "workspaces.id"),
        _fk("plan_id", "analysis_plans.id"),
        _fk("dataset_version_id", "dataset_versions.id"),
        sa.Column("semantic_model_version", sa.Integer, nullable=False),
        sa.Column("job_id", sa.Text, nullable=True),  # no FK: jobs are purged independently
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("steps", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dashboard", JSONB, nullable=True),
        _at("started_at"),
        _at("finished_at", nullable=True),
        sa.CheckConstraint(f"state IN ({RUN_STATES})", name="ck_analysis_runs_state"),
    )
    op.create_index(
        "ix_analysis_runs_version",
        "analysis_runs",
        ["workspace_id", "dataset_version_id", "semantic_model_version", "started_at"],
    )
    op.execute("ALTER TABLE evidence ADD COLUMN result_rows jsonb")
    op.execute("GRANT SELECT, INSERT ON analysis_plans TO analyzer_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON analysis_runs TO analyzer_app")
    op.execute("GRANT SELECT ON analysis_plans, analysis_runs TO analyzer_ro")
    for table in ("analysis_plans", "analysis_runs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} USING ({POLICY}) WITH CHECK ({POLICY})"
        )


def downgrade() -> None:
    op.drop_table("analysis_runs")
    op.drop_table("analysis_plans")
    op.execute("ALTER TABLE evidence DROP COLUMN result_rows")
