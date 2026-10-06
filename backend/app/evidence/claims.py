"""Append-only claim store (TASK-EVID-004, design 39 and 42).

``claims.evidence_ids`` is an array, so PostgreSQL cannot enforce the claim -> evidence FK; the
repository enforces it at insert time: every cited evidence id must exist in the caller's
workspace. There is no update or delete (the app role holds only INSERT/SELECT, migration 0004).

Column mapping: ``Claim.text`` -> ``text_span``; ``answer_id`` exists in the table but not in the
``Claim`` contract and is written as NULL until the contract gains it.
"""

from sqlalchemy import text

from app.core.errors import NotFoundError, SecurityError
from app.core.scope import WorkspaceScope
from app.evidence.domain import Claim
from app.evidence.repository import EvidenceRepository
from app.infra.repository import Repository


class ClaimRepository(Repository):
    def insert(self, scope: WorkspaceScope, claim: Claim) -> bool:
        """Insert once; returns False when the id already exists."""
        if claim.workspace_id != scope.workspace_id:
            raise SecurityError("claim workspace does not match the caller's scope")
        cited = EvidenceRepository(self._session).get_many(scope, claim.evidence_ids)
        if missing := sorted(set(claim.evidence_ids) - {e.evidence_id for e in cited}):
            raise NotFoundError("claim cites unknown evidence", details={"evidence_ids": missing})
        result = self._session.execute(
            text(
                "INSERT INTO claims (id, workspace_id, widget_id, kind, text_span, evidence_ids, "
                "validation_id, created_at) VALUES (:id, :ws, :widget, :kind, :text, :ev, :va, "
                ":at) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": claim.claim_id,
                "ws": claim.workspace_id,
                "widget": claim.widget_id,
                "kind": claim.kind,
                "text": claim.text,
                "ev": list(claim.evidence_ids),
                "va": claim.validation_id,
                "at": claim.created_at,
            },
        )
        return int(result.rowcount) == 1  # type: ignore[attr-defined]

    def get(self, scope: WorkspaceScope, claim_id: str) -> Claim | None:
        row = self._session.execute(
            text(
                "SELECT id, workspace_id, kind, text_span, widget_id, evidence_ids, "
                "validation_id, created_at FROM claims WHERE workspace_id = :ws AND id = :id"
            ),
            {"ws": scope.workspace_id, "id": claim_id},
        ).one_or_none()
        if row is None:
            return None
        return Claim(
            claim_id=row.id,
            workspace_id=row.workspace_id,
            kind=row.kind,
            text=row.text_span,
            widget_id=row.widget_id,
            evidence_ids=row.evidence_ids,
            validation_id=row.validation_id,
            created_at=row.created_at,
        )
