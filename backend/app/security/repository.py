"""Identity and session queries (TASK-SEC-004). Authentication runs before any workspace is
known, so these methods take the `SystemScope` held by the auth service.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app.core.scope import SystemScope
from app.infra.repository import Repository
from app.tools.permissions import Role


@dataclass(frozen=True)
class UserRecord:
    id: str
    email: str
    password_hash: str | None
    active: bool


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    user_id: str
    email: str
    workspace_id: str
    role: Role


class AuthRepository(Repository):
    def find_user(self, scope: SystemScope, email: str) -> UserRecord | None:
        row = self._session.execute(
            text(
                "SELECT id, email, password_hash, status FROM users "
                "WHERE lower(email) = lower(:email)"
            ),
            {"email": email},
        ).one_or_none()
        if row is None:
            return None
        return UserRecord(row.id, row.email, row.password_hash, row.status == "active")

    def memberships(self, scope: SystemScope, user_id: str) -> list[tuple[str, Role]]:
        rows = self._session.execute(
            text(
                "SELECT workspace_id, role FROM memberships WHERE user_id = :uid "
                "ORDER BY created_at, workspace_id"
            ),
            {"uid": user_id},
        )
        return [(r.workspace_id, Role(r.role)) for r in rows]

    def set_password_hash(self, scope: SystemScope, user_id: str, password_hash: str) -> None:
        self._session.execute(
            text("UPDATE users SET password_hash = :h WHERE id = :uid"),
            {"h": password_hash, "uid": user_id},
        )

    def create_session(
        self,
        scope: SystemScope,
        session_id: str,
        user_id: str,
        workspace_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        self._session.execute(
            text(
                "INSERT INTO sessions (id, user_id, workspace_id, created_at, last_seen_at, "
                "expires_at) VALUES (:id, :uid, :ws, :now, :now, :exp)"
            ),
            {"id": session_id, "uid": user_id, "ws": workspace_id, "now": now, "exp": expires_at},
        )

    def touch_active_session(
        self, scope: SystemScope, session_id: str, now: datetime, idle_cutoff: datetime
    ) -> SessionRecord | None:
        """Return the session if live (not revoked/expired/idle, user active, still a member)
        and bump `last_seen_at`. Role comes from the membership row, so changes apply at once."""
        row = self._session.execute(
            text(
                "UPDATE sessions s SET last_seen_at = :now "
                "FROM users u, memberships m "
                "WHERE s.id = :id AND s.revoked_at IS NULL AND s.expires_at > :now "
                "AND s.last_seen_at > :idle_cutoff "
                "AND u.id = s.user_id AND u.status = 'active' "
                "AND m.user_id = s.user_id AND m.workspace_id = s.workspace_id "
                "RETURNING s.id, s.user_id, u.email, s.workspace_id, m.role"
            ),
            {"id": session_id, "now": now, "idle_cutoff": idle_cutoff},
        ).one_or_none()
        if row is None:
            return None
        return SessionRecord(row.id, row.user_id, row.email, row.workspace_id, Role(row.role))

    def revoke_session(self, scope: SystemScope, session_id: str, now: datetime) -> None:
        self._session.execute(
            text("UPDATE sessions SET revoked_at = :now WHERE id = :id AND revoked_at IS NULL"),
            {"id": session_id, "now": now},
        )

    def workspace_exists(self, scope: SystemScope, workspace_id: str) -> bool:
        found = self._session.execute(
            text("SELECT 1 FROM workspaces WHERE id = :id"), {"id": workspace_id}
        ).scalar_one_or_none()
        return found is not None

    def create_workspace(self, scope: SystemScope, workspace_id: str, name: str) -> None:
        self._session.execute(
            text("INSERT INTO workspaces (id, name) VALUES (:id, :name)"),
            {"id": workspace_id, "name": name},
        )

    def create_user(self, scope: SystemScope, user_id: str, email: str, password_hash: str) -> None:
        self._session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :h)"),
            {"id": user_id, "email": email, "h": password_hash},
        )

    def add_membership(
        self, scope: SystemScope, user_id: str, workspace_id: str, role: Role
    ) -> None:
        self._session.execute(
            text("INSERT INTO memberships (user_id, workspace_id, role) VALUES (:uid, :ws, :role)"),
            {"uid": user_id, "ws": workspace_id, "role": role.value},
        )
