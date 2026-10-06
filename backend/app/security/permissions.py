"""Request-level RBAC (TASK-SEC-005, design §59).

Table-driven: a role grants a fixed permission set; `require(Permission.X)` in `app.api.deps`
checks it. Tool permissions (app.tools.permissions, DEC-017) are derived from the same matrix:
TRAIN_MODEL ⇔ MODEL_RUN and WRITE_MEMORY ⇔ MEMORY_WRITE (tested).
"""

from enum import StrEnum

from app.tools.permissions import Role


class Permission(StrEnum):
    VIEW_DATASET = "VIEW_DATASET"
    VIEW_QUERY = "VIEW_QUERY"
    UPLOAD_DATASET = "UPLOAD_DATASET"
    EDIT_SEMANTICS = "EDIT_SEMANTICS"
    RUN_ANALYSIS = "RUN_ANALYSIS"
    TRAIN_MODEL = "TRAIN_MODEL"
    ACCESS_DOCUMENTS = "ACCESS_DOCUMENTS"
    READ_MEMORY = "READ_MEMORY"
    WRITE_MEMORY = "WRITE_MEMORY"
    RUN_SIMULATION = "RUN_SIMULATION"
    EXECUTE_TOOL = "EXECUTE_TOOL"
    RUN_REPAIR = "RUN_REPAIR"
    APPROVE_REPAIR = "APPROVE_REPAIR"
    DEPLOY_MODEL = "DEPLOY_MODEL"
    MANAGE_USERS = "MANAGE_USERS"
    VIEW_AUDIT = "VIEW_AUDIT"


P = Permission
_VIEWER = frozenset(
    {
        P.VIEW_DATASET,
        P.RUN_ANALYSIS,
        P.EXECUTE_TOOL,
        P.ACCESS_DOCUMENTS,
        P.READ_MEMORY,
        P.RUN_SIMULATION,
    }
)
_ANALYST = _VIEWER | {
    P.VIEW_QUERY,
    P.UPLOAD_DATASET,
    P.EDIT_SEMANTICS,
    P.TRAIN_MODEL,
    P.WRITE_MEMORY,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.ANALYST: _ANALYST,
    Role.ADMIN: frozenset(Permission),
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
