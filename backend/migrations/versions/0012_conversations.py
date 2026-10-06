"""Chat conversations and messages (append-only).

Revision ID: 0012
Revises: 0010
Create Date: 2026-09-28

TASK-DB-008 (DES-API-002). Revision 0011 belongs to a concurrent stream; the chain is
re-linked at merge.

- ``conversations``: workspace-scoped and owned by the creating user. A conversation is bound
  to one dataset version + semantic model version, so every turn in it reads the same data.
- ``messages``: one row per user question and per assistant answer. ``segments`` holds the
  answer segments (text / evidence-bound number / caveat), ``state`` the final turn state,
  ``turn`` the persisted turn trace (state transitions, tool calls, budget use).
  ``validation_id`` links the answer's final ValidationResult.
The app role holds only INSERT/SELECT: messages are an immutable record of what was shown.
Both tables get the migration-0009 ``workspace_isolation`` RLS policy (ADR-0013).
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLES = "conversations, messages"
POLICY = (
    "current_setting('app.system', true) = 'on' "
    "OR workspace_id = current_setting('app.workspace_id', true)"
)


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
        "conversations",
        sa.Column("id", sa.Text, primary_key=True),
        _ws(),
        sa.Column(
            "owner_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "dataset_version_id", sa.Text, sa.ForeignKey("dataset_versions.id"), nullable=False
        ),
        sa.Column("semantic_model_version", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        _created_at(),
        sa.CheckConstraint("semantic_model_version >= 1", name="ck_conversations_sm_version"),
    )
    op.create_index(
        "ix_conversations_owner", "conversations", ["workspace_id", "owner_id", "created_at"]
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Text, primary_key=True),
        _ws(),
        sa.Column(
            "conversation_id",
            sa.Text,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("segments", JSONB, nullable=True),
        sa.Column("state", sa.Text, nullable=True),
        sa.Column("validation_id", sa.Text, sa.ForeignKey("validation_results.id"), nullable=True),
        sa.Column("turn", JSONB, nullable=True),
        _created_at(),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        sa.CheckConstraint(
            "(role = 'user') = (content IS NOT NULL AND segments IS NULL)",
            name="ck_messages_shape",
        ),
    )
    op.create_index("ix_messages_conversation", "messages", ["conversation_id", "created_at"])
    op.execute(f"GRANT SELECT, INSERT ON {TABLES} TO analyzer_app")
    op.execute(f"GRANT SELECT ON {TABLES} TO analyzer_ro")
    for table in ("conversations", "messages"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} USING ({POLICY}) WITH CHECK ({POLICY})"
        )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
