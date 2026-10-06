"""Append-only evidence store (TASK-EVID-003, DES-EVID-001).

There is deliberately no update or delete method, and the app role holds only INSERT/SELECT on
the table (migration 0004), so immutability holds even if this class is bypassed.
"""

from collections.abc import Sequence

from sqlalchemy import text

from app.core.errors import SecurityError
from app.core.scope import WorkspaceScope
from app.evidence.domain import Evidence
from app.evidence.preview import RowsPreview
from app.infra.repository import Repository


class EvidenceRepository(Repository):
    def insert(
        self, scope: WorkspaceScope, evidence: Evidence, rows: RowsPreview | None = None
    ) -> bool:
        """Insert once. Re-inserting the same (deterministic) id is a no-op; returns False.
        ``rows`` is the bounded result preview (app.evidence.preview), stored with the row."""
        if evidence.workspace_id != scope.workspace_id:
            raise SecurityError("evidence workspace does not match the caller's scope")
        result = self._session.execute(
            text(
                "INSERT INTO evidence (id, workspace_id, kind, dataset_version_id, operation, "
                "payload, result_hash, created_at, result_rows) VALUES (:id, :ws, :kind, :dsv, "
                ":op, CAST(:payload AS jsonb), :hash, :at, CAST(:rows AS jsonb)) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": evidence.evidence_id,
                "ws": evidence.workspace_id,
                "kind": evidence.kind,
                "dsv": evidence.dataset_version_id,
                "op": evidence.operation,
                "payload": evidence.model_dump_json(),
                "hash": evidence.result.result_hash,
                "at": evidence.created_at,
                "rows": None if rows is None else rows.model_dump_json(),
            },
        )
        return int(result.rowcount) == 1  # type: ignore[attr-defined]

    def get(self, scope: WorkspaceScope, evidence_id: str) -> Evidence | None:
        found = self.get_many(scope, [evidence_id])
        return found[0] if found else None

    def rows(self, scope: WorkspaceScope, evidence_id: str) -> tuple[bool, RowsPreview | None]:
        """(evidence exists in this workspace, its stored result preview)."""
        row = self._session.execute(
            text("SELECT result_rows FROM evidence WHERE workspace_id = :ws AND id = :id"),
            {"ws": scope.workspace_id, "id": evidence_id},
        ).one_or_none()
        if row is None:
            return False, None
        stored = row.result_rows
        return True, None if stored is None else RowsPreview.model_validate(stored)

    def get_many(self, scope: WorkspaceScope, evidence_ids: Sequence[str]) -> list[Evidence]:
        rows = self._session.execute(
            text(
                "SELECT payload FROM evidence WHERE workspace_id = :ws AND id = ANY(:ids) "
                "ORDER BY created_at, id"
            ),
            {"ws": scope.workspace_id, "ids": list(evidence_ids)},
        ).scalars()
        return [Evidence.model_validate(p) for p in rows]
