import pytest

from app.core.scope import SystemScope, WorkspaceScope
from app.infra.repository import Repository


def test_workspace_scope_validates_ids() -> None:
    WorkspaceScope(workspace_id="ws_0a1b2c", principal_id="usr_1")
    with pytest.raises(ValueError, match="workspace id"):
        WorkspaceScope(workspace_id="ws_../x", principal_id="usr_1")
    with pytest.raises(ValueError, match="principal_id"):
        WorkspaceScope(workspace_id="ws_0a1b2c", principal_id="")


def test_system_scope_requires_prefix() -> None:
    SystemScope("system:reaper")
    with pytest.raises(ValueError, match="system:"):
        SystemScope("reaper")


def test_repository_with_scoped_methods_is_accepted() -> None:
    class Good(Repository):
        def get(self, scope: WorkspaceScope, item_id: str) -> None: ...

        def _helper(self) -> None: ...

    assert issubclass(Good, Repository)


def test_repository_without_scope_is_rejected_at_definition() -> None:
    with pytest.raises(TypeError, match="must take `scope`"):

        class Bad(Repository):
            def get_all(self) -> None: ...


def test_scope_must_be_first_argument() -> None:
    with pytest.raises(TypeError, match="must take `scope`"):

        class Bad(Repository):
            def get(self, item_id: str, scope: WorkspaceScope) -> None: ...
