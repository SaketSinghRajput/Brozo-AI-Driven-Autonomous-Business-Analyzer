"""Dataset memory: datasets, dataset_versions, profiles, understandings, semantic models.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

TASK-DB-003 (TRD §31, design §9, ADR-0023). Versioned rows are insert-only; the only
mutable column is `dataset_versions.state`, advanced by compare-and-set (design §9).
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

STATES = (
    "UPLOADED",
    "VALIDATING",
    "VALIDATION_FAILED",
    "INGESTING",
    "INGESTION_FAILED",
    "INGESTED",
    "PROFILED",
    "SCHEMA_ANALYZED",
    "DOMAIN_IDENTIFIED",
    "SEMANTICALLY_MAPPED",
    "ANALYZING",
    "READY",
    "ANALYSIS_FAILED",
)


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _fk(name: str, target: str) -> sa.Column[Any]:
    return sa.Column(name, sa.Text, sa.ForeignKey(target, ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.Text, primary_key=True),
        _fk("workspace_id", "workspaces.id"),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, sa.ForeignKey("users.id"), nullable=True),
        _created_at(),
    )
    op.create_index("ix_datasets_workspace_created", "datasets", ["workspace_id", "created_at"])

    state_list = ", ".join(f"'{s}'" for s in STATES)
    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.Text, primary_key=True),
        _fk("workspace_id", "workspaces.id"),
        _fk("dataset_id", "datasets.id"),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("source_sha256", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=True),  # set once the Parquet is committed
        sa.Column("storage_uri", sa.Text, nullable=True),
        sa.Column("row_count", sa.BigInteger, nullable=True),
        sa.Column("parent_version_id", sa.Text, sa.ForeignKey("dataset_versions.id")),
        sa.Column("state", sa.Text, nullable=False, server_default="UPLOADED"),
        sa.Column("failure", JSONB, nullable=True),
        _created_at(),
        sa.CheckConstraint("version_no >= 1", name="ck_dataset_versions_version_no"),
        sa.CheckConstraint(f"state IN ({state_list})", name="ck_dataset_versions_state"),
        sa.UniqueConstraint("dataset_id", "version_no", name="uq_dataset_versions_no"),
        sa.UniqueConstraint("dataset_id", "content_hash", name="uq_dataset_versions_content"),
        sa.UniqueConstraint("dataset_id", "source_sha256", name="uq_dataset_versions_source"),
    )
    op.create_index(
        "ix_dataset_versions_workspace_created", "dataset_versions", ["workspace_id", "created_at"]
    )

    derived = {
        "dataset_profiles": [sa.Column("profile", JSONB, nullable=False)],
        "understandings": [
            sa.Column("understanding", JSONB, nullable=False),
            sa.Column("domain_result", JSONB, nullable=True),
        ],
    }
    for table, columns in derived.items():
        op.create_table(
            table,
            _fk("dataset_version_id", "dataset_versions.id"),
            _fk("workspace_id", "workspaces.id"),
            sa.Column("engine_version", sa.Text, nullable=False),
            *columns,
            _created_at(),
            sa.PrimaryKeyConstraint("dataset_version_id", "engine_version"),
        )

    op.create_table(
        "semantic_models",
        sa.Column("id", sa.Text, primary_key=True),
        _fk("workspace_id", "workspaces.id"),
        _fk("dataset_version_id", "dataset_versions.id"),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("model", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, sa.ForeignKey("users.id"), nullable=True),
        _created_at(),
        sa.UniqueConstraint("dataset_version_id", "version", name="uq_semantic_models_version"),
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON datasets TO analyzer_app")
    # Versioned/derived rows are insert-only; dataset_versions allows UPDATE for the state CAS.
    op.execute("GRANT SELECT, INSERT, UPDATE ON dataset_versions TO analyzer_app")
    op.execute(
        "GRANT SELECT, INSERT ON dataset_profiles, understandings, semantic_models TO analyzer_app"
    )
    op.execute(
        "GRANT SELECT ON datasets, dataset_versions, dataset_profiles, understandings, "
        "semantic_models TO analyzer_ro"
    )


def downgrade() -> None:
    for table in (
        "semantic_models",
        "understandings",
        "dataset_profiles",
        "dataset_versions",
        "datasets",
    ):
        op.drop_table(table)
