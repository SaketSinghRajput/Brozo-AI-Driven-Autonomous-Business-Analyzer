"""Request dependencies shared by feature routers.

`get_container` is the only place request handlers reach the composition root.
`current_principal` resolves the session cookie (401 without a live session) and, for unsafe
methods, checks the CSRF header (403). The workspace scope comes only from the session, whose
membership is re-checked on every request (ADR-0013). Every non-public route declares
`require(Permission.X)`, which returns the caller's `WorkspaceScope` (enforced by a route test).
"""

from typing import Annotated

from fastapi import Depends, Request

from app.core.container import Container
from app.core.scope import WorkspaceScope
from app.security.auth import CSRF_HEADER, SAFE_METHODS, Principal
from app.security.permissions import Permission


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def current_principal(request: Request, container: ContainerDep) -> Principal:
    auth = container.auth
    token = request.cookies.get(auth.cookie_name)
    principal = auth.authenticate(token)
    if request.method not in SAFE_METHODS:
        auth.check_csrf(principal, token or "", request.headers.get(CSRF_HEADER))
    return principal


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def current_scope(principal: PrincipalDep) -> WorkspaceScope:
    return principal.scope


ScopeDep = Annotated[WorkspaceScope, Depends(current_scope)]


class require:  # lower-case: reads as a function at call sites, Depends(require(P.X))
    """Dependency factory: 403 unless the session's role grants `permission`."""

    def __init__(self, permission: Permission) -> None:
        self.permission = permission

    def __call__(self, principal: PrincipalDep, container: ContainerDep) -> WorkspaceScope:
        return container.auth.authorize(principal, self.permission)
