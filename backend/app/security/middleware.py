"""Boundary B1 middleware (TASK-SEC-007): security headers and request body size limits.

CSRF is enforced in the authentication dependency (app.api.deps), where the session is known.
"""

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

UPLOAD_PATH_PREFIXES = ("/api/v1/datasets",)  # multipart uploads get the storage limit
_DOCS_PREFIX = "/api/docs"  # Swagger UI (non-prod only) loads its own assets

_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
_HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


class SecurityHeadersMiddleware:
    """Hardening headers on every HTTP response. The API only serves JSON, so its CSP is
    `default-src 'none'`; the SPA's own CSP belongs to the static server (nginx)."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = list(_HEADERS)
        if not scope["path"].startswith(_DOCS_PREFIX):
            extra += [_API_CSP, (b"cache-control", b"no-store")]
        if self.hsts:
            extra.append(_HSTS)
        names = {name for name, _ in extra}

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message["headers"] = kept + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyLimitMiddleware:
    """Rejects bodies over the limit with 413, whether declared by Content-Length or streamed.

    Raising Starlette's HTTPException from `receive` makes FastAPI answer with the standard error
    envelope (PAYLOAD_TOO_LARGE); the oversized body is never buffered past the limit.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, max_upload_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_upload_bytes = max_upload_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_upload = scope["path"].startswith(UPLOAD_PATH_PREFIXES)
        limit = self.max_upload_bytes if is_upload else self.max_body_bytes
        declared = dict(scope.get("headers") or []).get(b"content-length", b"")
        too_large = declared.isdigit() and int(declared) > limit
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            if too_large:
                raise HTTPException(413, "request body too large")
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "request body too large")
            return message

        await self.app(scope, limited_receive, send)
