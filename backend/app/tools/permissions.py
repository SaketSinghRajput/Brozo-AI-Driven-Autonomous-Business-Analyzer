"""Tool permissions per role and turn profile (TASK-TOOL-002, design §20 and §59).

Effective tool permissions = role ∩ turn profile; the executor then checks the tool's own
requirement. Role grants are derived from the design §59 matrix: Viewer gets read-only tools,
Analyst adds per-analysis model fitting (TRAIN_MODEL "per-analysis" → MODEL_RUN) and memory
writes, Admin gets everything. The full request-level `Permission` enum and `require()`
dependency are TASK-SEC-005 (P1); only the tool slice lives here.
"""

from enum import StrEnum

from app.tools.domain import ToolPermission as P


class Role(StrEnum):  # values match the memberships.role check constraint
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"


class TurnProfile(StrEnum):
    CHAT_DEFAULT = "chat_default"
    CHAT_WITH_MEMORY_WRITE = "chat_with_memory_write"
    DASHBOARD_SYSTEM = "dashboard_system"
    REPAIR_PIPELINE = "repair_pipeline"


_READ_ONLY = frozenset(
    {P.SQL_READ, P.PYTHON_ANALYTICS, P.DOCUMENT_SEARCH, P.MEMORY_READ, P.SIMULATION}
)

ROLE_TOOL_PERMISSIONS: dict[Role, frozenset[P]] = {
    Role.VIEWER: _READ_ONLY,
    Role.ANALYST: _READ_ONLY | {P.MODEL_RUN, P.MEMORY_WRITE},
    Role.ADMIN: frozenset(P),
}

_CHAT = frozenset(
    {P.SQL_READ, P.PYTHON_ANALYTICS, P.MODEL_RUN, P.DOCUMENT_SEARCH, P.MEMORY_READ, P.SIMULATION}
)

PROFILE_TOOL_PERMISSIONS: dict[TurnProfile, frozenset[P]] = {
    TurnProfile.CHAT_DEFAULT: _CHAT,
    TurnProfile.CHAT_WITH_MEMORY_WRITE: _CHAT | {P.MEMORY_WRITE},
    TurnProfile.DASHBOARD_SYSTEM: frozenset({P.SQL_READ, P.PYTHON_ANALYTICS, P.MODEL_RUN}),
    TurnProfile.REPAIR_PIPELINE: frozenset({P.CODE_EXECUTION, P.PATCH_PROPOSE}),  # no data access
}


def effective_permissions(role: Role, profile: TurnProfile) -> frozenset[P]:
    return ROLE_TOOL_PERMISSIONS[role] & PROFILE_TOOL_PERMISSIONS[profile]
