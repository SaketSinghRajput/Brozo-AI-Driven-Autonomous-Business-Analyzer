"""Domain classifications per dataset version (detector result + user overrides).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-28

TASK-DOMAIN-006/007 (design §9, §14). Insert-only like the other derived rows: seq 1 is the
detector's result (written once, idempotent), every user override inserts seq = max + 1 and the
highest seq is the version's current classification. `understandings.domain_result` (0002) stays
unused: understandings are written at SCHEMA_ANALYZED, before the domain is known, and are
insert-only.
Row-Level Security: the same `workspace_isolation` policy as 0009 (TASK-DB-012).
NOTE: originally chained after 0008; re-chained after 0009 (security hardening) at merge.
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

POLICY = (
    "current_setting('app.system', true) = 'on' "
    "OR workspace_id = current_setting('app.workspace_id', true)"
)


def _fk(name: str, target: str) -> sa.Column[Any]:
    return sa.Column(name, sa.Text, sa.ForeignKey(target, ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "domain_classifications",
        _fk("dataset_version_id", "dataset_versions.id"),
        _fk("workspace_id", "workspaces.id"),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("classification", JSONB, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=True),  # principal id (no FK: system actors)
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("dataset_version_id", "seq"),
        sa.CheckConstraint("seq >= 1", name="ck_domain_classifications_seq"),
        sa.CheckConstraint(
            "source IN ('detector', 'user_override')", name="ck_domain_classifications_source"
        ),
    )
    op.execute("ALTER TABLE domain_classifications ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON domain_classifications USING ({POLICY}) "
        f"WITH CHECK ({POLICY})"
    )
    op.execute("GRANT SELECT, INSERT ON domain_classifications TO analyzer_app")
    op.execute("GRANT SELECT ON domain_classifications TO analyzer_ro")


def downgrade() -> None:
    op.drop_table("domain_classifications")
