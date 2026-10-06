"""Allow `unavailable` as a tool_runs status (ToolResult has three outcomes).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28

TASK-TOOL-004: an honest "cannot compute" (Unavailability) is not an error and must be
recorded as such.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_tool_runs_status", "tool_runs", type_="check")
    op.create_check_constraint(
        "ck_tool_runs_status", "tool_runs", "status IN ('ok', 'unavailable', 'error')"
    )


def downgrade() -> None:
    """Map `unavailable` runs to `error` (error_code UNAVAILABLE) so the old check can return.

    DEBT-007: without this, the downgrade failed on any database holding an `unavailable` run.
    Lossy by design: the pre-0005 schema cannot express "cannot compute", and deleting the rows
    would break evidence links. Editing a released migration is acceptable here because no
    production database exists yet (P1); every environment is a disposable dev/test database.
    """
    op.execute(
        "UPDATE tool_runs SET status = 'error', error_code = COALESCE(error_code, 'UNAVAILABLE') "
        "WHERE status = 'unavailable'"
    )
    op.drop_constraint("ck_tool_runs_status", "tool_runs", type_="check")
    op.create_check_constraint("ck_tool_runs_status", "tool_runs", "status IN ('ok', 'error')")
