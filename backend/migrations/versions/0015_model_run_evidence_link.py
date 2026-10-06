"""Let the app role link a model run to its evidence (GAPS-6).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-04

The EvidenceRecorder writes a model run's evidence after the ML tool stored the run, then sets
`model_runs.evidence_id` in the same transaction. The app role gets UPDATE on that one column
only, so every other column (the run record itself) stays immutable at the privilege level; the
workspace RLS policy of migration 0009 already covers UPDATE. The repository only fills a NULL
link (`WHERE evidence_id IS NULL`).
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT UPDATE (evidence_id) ON model_runs TO analyzer_app")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (evidence_id) ON model_runs FROM analyzer_app")
