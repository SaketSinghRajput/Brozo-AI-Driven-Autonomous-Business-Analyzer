"""Tenant scopes. Every repository call carries one (TRD section 31.3, design section 50).

`WorkspaceScope` limits data access to one workspace. `SystemScope` is a capability object held
only by system components (migrations, reapers, admin jobs) and must never be built from user input.
"""

import re
from dataclasses import dataclass

_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{6,32}$")


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    workspace_id: str
    principal_id: str

    def __post_init__(self) -> None:
        if not _WORKSPACE_ID.match(self.workspace_id):
            raise ValueError("invalid workspace id")
        if not self.principal_id:
            raise ValueError("principal_id is required")


@dataclass(frozen=True, slots=True)
class SystemScope:
    component: str

    def __post_init__(self) -> None:
        if not self.component.startswith("system:"):
            raise ValueError("SystemScope component must start with 'system:'")


type Scope = WorkspaceScope | SystemScope
