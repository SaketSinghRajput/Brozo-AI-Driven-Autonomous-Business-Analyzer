"""TASK-TOOL-002/003: effective permissions and the tool registry (no database)."""

import pytest
from pydantic import BaseModel

from app.core.errors import ToolError
from app.planner.domain import Unavailability
from app.tools.domain import ToolContext, ToolMetadata
from app.tools.domain import ToolPermission as P
from app.tools.permissions import (
    PROFILE_TOOL_PERMISSIONS,
    Role,
    TurnProfile,
    effective_permissions,
)
from app.tools.registry import ToolRegistry


class Args(BaseModel):
    column: str


class Out(BaseModel):
    value: float


def make_tool(name: str, permission: P) -> object:
    class _Tool:
        meta = ToolMetadata(
            name=name,
            version="1.0.0",
            description=f"test tool {name}",
            permission=permission,
            side_effects="none",
            timeout_s=1,
            max_calls_per_turn=1,
            parallel_safe=True,
            runs_in="api",
        )
        input_model = Args
        output_model = Out

        async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability:
            return Out(value=1)

    return _Tool()


@pytest.mark.parametrize(
    ("role", "profile", "has", "lacks"),
    [
        (Role.VIEWER, TurnProfile.CHAT_DEFAULT, {P.SQL_READ, P.SIMULATION}, {P.MODEL_RUN}),
        (Role.ANALYST, TurnProfile.CHAT_DEFAULT, {P.MODEL_RUN}, {P.MEMORY_WRITE, P.MODEL_TRAIN}),
        (Role.ANALYST, TurnProfile.CHAT_WITH_MEMORY_WRITE, {P.MEMORY_WRITE}, {P.CODE_EXECUTION}),
        (Role.ADMIN, TurnProfile.DASHBOARD_SYSTEM, {P.SQL_READ, P.MODEL_RUN}, {P.MEMORY_READ}),
        (Role.ADMIN, TurnProfile.REPAIR_PIPELINE, {P.CODE_EXECUTION}, {P.SQL_READ, P.MODEL_RUN}),
        (Role.VIEWER, TurnProfile.REPAIR_PIPELINE, set(), {P.CODE_EXECUTION, P.PATCH_PROPOSE}),
    ],
)
def test_effective_permissions_are_role_and_profile_intersection(
    role: Role, profile: TurnProfile, has: set[P], lacks: set[P]
) -> None:
    effective = effective_permissions(role, profile)
    assert has <= effective
    assert not (lacks & effective)
    assert effective <= PROFILE_TOOL_PERMISSIONS[profile]


def test_no_chat_profile_ever_grants_code_execution() -> None:
    for role in Role:
        for profile in (TurnProfile.CHAT_DEFAULT, TurnProfile.CHAT_WITH_MEMORY_WRITE):
            assert P.CODE_EXECUTION not in effective_permissions(role, profile)


def test_registry_get_register_and_unknown() -> None:
    tool = make_tool("analytics.sum", P.SQL_READ)
    registry = ToolRegistry([tool])  # type: ignore[list-item]
    assert registry.get("analytics.sum") is tool
    with pytest.raises(ToolError, match="unknown tool"):
        registry.get("analytics.nope")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_tool("analytics.sum", P.SQL_READ))  # type: ignore[arg-type]


def test_specs_for_filters_by_permission_and_exposes_input_schema() -> None:
    registry = ToolRegistry(
        [make_tool("analytics.sum", P.SQL_READ), make_tool("ml.forecast", P.MODEL_RUN)]  # type: ignore[list-item]
    )
    viewer = effective_permissions(Role.VIEWER, TurnProfile.CHAT_DEFAULT)
    specs = registry.specs_for(viewer)
    assert [s.name for s in specs] == ["analytics.sum"]
    assert specs[0].input_schema["properties"] == {"column": {"title": "Column", "type": "string"}}
    analyst = effective_permissions(Role.ANALYST, TurnProfile.CHAT_DEFAULT)
    assert [s.name for s in registry.specs_for(analyst)] == ["analytics.sum", "ml.forecast"]
