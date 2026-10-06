"""Test helper: a session principal for a scope, to override `current_principal`."""

from app.core.scope import WorkspaceScope
from app.security.auth import Principal
from app.tools.permissions import Role


def principal_for(scope: WorkspaceScope, role: Role = Role.ANALYST) -> Principal:
    return Principal(
        user_id=scope.principal_id,
        email="principal@example.test",
        workspace_id=scope.workspace_id,
        role=role,
        session_id="sess_test",
    )
