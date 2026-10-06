"""Login, session resolution, CSRF and permission checks (TASK-SEC-004/005/007, ADR-0013).

- Session: opaque 256-bit random token in the cookie; the database stores only its SHA-256, so
  a DB read yields no usable cookie. A fresh token is issued on every login (no fixation), and
  a session presented at login is revoked.
- Liveness is checked on every request: not revoked, before the absolute expiry, within the
  idle timeout, user active, and the user still a member of the session's workspace.
- CSRF: synchronizer token bound to the session, `HMAC-SHA256(session_secret, "csrf:" + token)`.
  It is never stored, so `/auth/me` can re-issue it after a page reload; forging it needs both
  the server secret and the HttpOnly session token.
- Unknown user, wrong password and disabled user share one error and one Argon2 verification.
- Throttling (M2): failures are counted per lower-cased email and per client IP; at the limit a
  login is refused with 429 before any database or Argon2 work, whether or not the email exists.
  Denial audit rows are rate-limited per principal (`WindowCounter`, per process).
"""

import hashlib
import hmac
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from app.core.clock import Clock
from app.core.config import SecurityConfig
from app.core.errors import AuthenticationError, AuthorizationError, RateLimitedError
from app.core.ids import IdGenerator
from app.core.scope import Scope, SystemScope, WorkspaceScope
from app.infra.db import UnitOfWork
from app.security.audit import AuditKeys, AuditLog, AuditResult, MetadataValue
from app.security.passwords import MAX_PASSWORD_LENGTH, PasswordHasher
from app.security.permissions import Permission, has_permission
from app.security.repository import AuthRepository
from app.security.throttle import WindowCounter
from app.tools.permissions import Role

log = logging.getLogger("app.security")

AUTH = SystemScope("system:auth")
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_INVALID_LOGIN = "invalid email or password"
_MAX_TOKEN_LENGTH = 128


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    workspace_id: str
    role: Role
    session_id: str

    @property
    def scope(self) -> WorkspaceScope:
        return WorkspaceScope(workspace_id=self.workspace_id, principal_id=self.user_id)

    @property
    def session_ref(self) -> str:  # audit reference; a hash prefix reveals nothing usable
        return f"ses_{self.session_id[:16]}"


@dataclass(frozen=True)
class LoginResult:
    token: str
    principal: Principal


class LoginThrottledError(RateLimitedError):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__("too many failed login attempts; try again later")
        self.retry_after_s = retry_after_s


def session_id_for(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(
        self, uow: UnitOfWork, clock: Clock, ids: IdGenerator, cfg: SecurityConfig
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._cfg = cfg
        self._secret = cfg.session_secret.get_secret_value().encode()
        self.hasher = PasswordHasher(cfg)
        self.audit_keys = AuditKeys.from_config(cfg)
        window = cfg.login_failure_window_s
        self._email_failures = WindowCounter(cfg.login_max_failures_per_email, window, clock)
        self._ip_failures = WindowCounter(cfg.login_max_failures_per_ip, window, clock)
        self._denials = WindowCounter(
            cfg.denial_audit_max_per_principal, cfg.denial_audit_window_s, clock
        )
        # `__Host-` binds the cookie to this origin; browsers accept it only with Secure.
        self.cookie_name = "__Host-session" if cfg.cookie_secure else "session"

    def csrf_token(self, token: str) -> str:
        return hmac.new(self._secret, b"csrf:" + token.encode(), hashlib.sha256).hexdigest()

    def login(
        self,
        email: str,
        password: str,
        *,
        workspace_id: str | None = None,
        previous_token: str | None = None,
        client_ip: str | None = None,
    ) -> LoginResult:
        email_key, ip_key = f"email:{email.lower()}", f"ip:{client_ip or 'unknown'}"
        wait = max(
            self._email_failures.retry_after_s(email_key),
            self._ip_failures.retry_after_s(ip_key),
        )
        if wait:
            log.warning("auth.login_throttled", extra={"fields": {"retry_after_s": wait}})
            raise LoginThrottledError(wait)
        now = self._clock.now()
        password = password[:MAX_PASSWORD_LENGTH]
        with self._uow(AUTH) as s:
            repo = AuthRepository(s)
            user = repo.find_user(AUTH, email)
            verified = self.hasher.verify(user.password_hash if user else None, password)
            actor = user.id if user else "anonymous"
            reason = "bad_credentials"
            if user is not None and verified and user.active:
                chosen = next(
                    (m for m in repo.memberships(AUTH, user.id) if workspace_id in (None, m[0])),
                    None,
                )
                reason = "no_membership"
                if chosen is not None:
                    if previous_token:
                        repo.revoke_session(AUTH, session_id_for(previous_token), now)
                    if user.password_hash and self.hasher.needs_rehash(user.password_hash):
                        repo.set_password_hash(AUTH, user.id, self.hasher.hash(password))
                    token = secrets.token_urlsafe(32)
                    principal = Principal(
                        user.id, user.email, chosen[0], chosen[1], session_id_for(token)
                    )
                    expires_at = now + timedelta(seconds=self._cfg.session_ttl_s)
                    repo.create_session(
                        AUTH, principal.session_id, user.id, chosen[0], now, expires_at
                    )
                    AuditLog(s, self.audit_keys, self._clock, self._ids).append(
                        principal.scope,
                        action="auth.login",
                        resource=principal.session_ref,
                        result="success",
                    )
                    self._email_failures.reset(email_key)
                    return LoginResult(token=token, principal=principal)
            elif user is not None and verified:
                reason = "disabled"
        # Denials are recorded in their own transaction (the one above has nothing to keep).
        self._audit(
            AUTH, "auth.login", "session", "denied", actor=actor, metadata={"reason": reason}
        )
        if reason == "no_membership":  # the credentials were valid: say what is wrong
            raise AuthorizationError("no access to the requested workspace")
        self._email_failures.hit(email_key)
        self._ip_failures.hit(ip_key)
        raise AuthenticationError(_INVALID_LOGIN)

    def authenticate(self, token: str | None) -> Principal:
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            raise AuthenticationError("authentication required")
        now = self._clock.now()
        idle_cutoff = now - timedelta(seconds=self._cfg.session_idle_s)
        with self._uow(AUTH) as s:
            record = AuthRepository(s).touch_active_session(
                AUTH, session_id_for(token), now, idle_cutoff
            )
        if record is None:
            raise AuthenticationError("authentication required")
        return Principal(
            record.user_id, record.email, record.workspace_id, record.role, record.session_id
        )

    def check_csrf(self, principal: Principal, token: str, presented: str | None) -> None:
        expected = self.csrf_token(token)
        if presented is None or not hmac.compare_digest(presented.encode(), expected.encode()):
            self._audit_denial(principal, "csrf.check", principal.session_ref)
            raise AuthorizationError("CSRF token missing or invalid")

    def authorize(self, principal: Principal, permission: Permission) -> WorkspaceScope:
        if not has_permission(principal.role, permission):
            self._audit_denial(
                principal, "authz.check", permission.value, {"role": principal.role.value}
            )
            raise AuthorizationError("permission denied")
        return principal.scope

    def logout(self, principal: Principal) -> None:
        with self._uow(principal.scope) as s:
            AuthRepository(s).revoke_session(AUTH, principal.session_id, self._clock.now())
            AuditLog(s, self.audit_keys, self._clock, self._ids).append(
                principal.scope,
                action="auth.logout",
                resource=principal.session_ref,
                result="success",
            )

    def _audit_denial(
        self,
        principal: Principal,
        action: str,
        resource: str,
        metadata: Mapping[str, MetadataValue] | None = None,
    ) -> None:
        """Audit a denial unless this principal exceeded its denial-audit budget (M2)."""
        if self._denials.hit(f"{principal.workspace_id}:{principal.user_id}") > self._denials.limit:
            log.warning(
                "audit.denial_suppressed",
                extra={"fields": {"action": action, "workspace_id": principal.workspace_id}},
            )
            return
        self._audit(principal.scope, action, resource, "denied", metadata=metadata)

    def _audit(
        self,
        scope: Scope,
        action: str,
        resource: str,
        result: AuditResult,
        *,
        actor: str | None = None,
        metadata: Mapping[str, MetadataValue] | None = None,
    ) -> None:
        with self._uow(scope) as s:
            AuditLog(s, self.audit_keys, self._clock, self._ids).append(
                scope,
                action=action,
                resource=resource,
                result=result,
                actor=actor,
                metadata=metadata,
            )
