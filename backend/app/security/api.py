"""Auth API (TASK-SEC-004) and audit verification trigger (TASK-SEC-012)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.deps import ContainerDep, PrincipalDep, require
from app.core.container import Container
from app.core.errors import ApplicationError
from app.core.logging import trace_id_var
from app.core.scope import WorkspaceScope
from app.jobs.models import Job, JobType
from app.jobs.repository import JobRepository
from app.security.auth import LoginThrottledError, Principal
from app.security.passwords import MAX_PASSWORD_LENGTH
from app.security.permissions import Permission
from app.tools.permissions import Role

router = APIRouter(prefix="/auth", tags=["auth"])
audit_router = APIRouter(prefix="/audit", tags=["audit"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    workspace_id: str | None = Field(default=None, pattern=r"^ws_[0-9a-f]{6,32}$")


class SessionInfo(BaseModel):
    user_id: str
    email: str
    workspace_id: str
    role: Role
    csrf_token: str  # send back as X-CSRF-Token on POST/PUT/PATCH/DELETE


async def throttled_response(request: Request, exc: Exception) -> Response:
    """Standard error envelope for LoginThrottledError, plus the Retry-After header."""
    envelope = request.app.exception_handlers[ApplicationError]
    response: JSONResponse = await envelope(request, exc)
    if isinstance(exc, LoginThrottledError):
        response.headers["Retry-After"] = str(exc.retry_after_s)
    return response


def _info(container: Container, principal: Principal, token: str) -> SessionInfo:
    return SessionInfo(
        user_id=principal.user_id,
        email=principal.email,
        workspace_id=principal.workspace_id,
        role=principal.role,
        csrf_token=container.auth.csrf_token(token),
    )


@router.post("/login")
def login(
    body: LoginRequest, request: Request, response: Response, container: ContainerDep
) -> SessionInfo:
    """Public. Sets the HttpOnly session cookie (a new session id on every login) and returns
    the CSRF token. Wrong email and wrong password give the same 401."""
    auth = container.auth
    result = auth.login(
        body.email,
        body.password.get_secret_value(),
        workspace_id=body.workspace_id,
        previous_token=request.cookies.get(auth.cookie_name),
        # ponytail: behind a reverse proxy this is the proxy's address unless uvicorn runs with
        # --proxy-headers/--forwarded-allow-ips; the per-IP limit then becomes a global one.
        client_ip=request.client.host if request.client else None,
    )
    response.set_cookie(
        auth.cookie_name,
        result.token,
        max_age=container.config.security.session_ttl_s,
        path="/",
        secure=container.config.security.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return _info(container, result.principal, result.token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(principal: PrincipalDep, response: Response, container: ContainerDep) -> None:
    """Revokes the session server-side and clears the cookie. Requires the CSRF header."""
    container.auth.logout(principal)
    response.delete_cookie(
        container.auth.cookie_name,
        path="/",
        secure=container.config.security.cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.get("/me")
def me(principal: PrincipalDep, request: Request, container: ContainerDep) -> SessionInfo:
    """The current user, workspace and role, plus the CSRF token (for SPA reloads)."""
    token = request.cookies.get(container.auth.cookie_name, "")
    return _info(container, principal, token)


@audit_router.post("/verify", status_code=status.HTTP_202_ACCEPTED)
def verify_audit_chain(
    scope: Annotated[WorkspaceScope, Depends(require(Permission.VIEW_AUDIT))],
    container: ContainerDep,
) -> Job:
    """Enqueues an `audit_verify` job for the caller's workspace; poll GET /jobs/{id}."""
    with container.uow(scope) as s:
        return JobRepository(s).enqueue(
            scope,
            container.ids.new("job"),
            JobType.AUDIT_VERIFY,
            {},
            max_attempts=1,
            trace_id=trace_id_var.get(),
        )
