"""Error hierarchy and the standard error envelope (TRD section 35, design.md section 45).

Clients never receive stack traces, SQL, paths or secrets; details go to logs keyed by trace_id.
"""

import logging
from datetime import datetime
from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.clock import Clock
from app.core.logging import TRACE_HEADER, trace_id_var

log = logging.getLogger("app.errors")


class ErrorEnvelope(BaseModel):
    error_code: str
    message: str
    details: dict[str, Any] | None = None
    retryable: bool
    trace_id: str | None
    timestamp: datetime


class ApplicationError(Exception):
    code: ClassVar[str] = "SYSTEM_ERROR"
    http_status: ClassVar[int] = 500
    default_retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.retryable = self.default_retryable if retryable is None else retryable


class DataError(ApplicationError):
    code, http_status = "DATA_ERROR", 422


class SchemaError(ApplicationError):
    code, http_status = "SCHEMA_ERROR", 422


class AppValidationError(ApplicationError):
    code, http_status = "VALIDATION_ERROR", 422


class NotFoundError(ApplicationError):
    code, http_status = "NOT_FOUND", 404


class StateConflictError(ApplicationError):
    code, http_status = "STATE_CONFLICT", 409


class ModelError(ApplicationError):
    code, http_status = "MODEL_ERROR", 422


class LLMError(ApplicationError):
    code, http_status, default_retryable = "LLM_ERROR", 503, True


class ToolError(ApplicationError):
    code, http_status = "TOOL_ERROR", 422


class DatabaseError(ApplicationError):
    code, http_status, default_retryable = "DATABASE_ERROR", 503, True


class RAGError(ApplicationError):
    code, http_status = "RAG_ERROR", 503


class AuthenticationError(ApplicationError):
    code, http_status = "AUTH_ERROR", 401


class AuthorizationError(ApplicationError):
    code, http_status = "AUTH_ERROR", 403


class SecurityError(ApplicationError):
    code, http_status = "SECURITY_ERROR", 400


class RateLimitedError(ApplicationError):
    code, http_status, default_retryable = "RATE_LIMITED", 429, True


_HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE"}


def install_error_handlers(app: FastAPI, clock: Clock) -> None:
    def respond(
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> JSONResponse:
        trace_id = trace_id_var.get()
        body = ErrorEnvelope(
            error_code=code,
            message=message,
            details=details,
            retryable=retryable,
            trace_id=trace_id,
            timestamp=clock.now(),
        )
        headers = {TRACE_HEADER: trace_id} if trace_id else None
        return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=headers)

    @app.exception_handler(ApplicationError)
    async def _app_error(_: Request, exc: ApplicationError) -> JSONResponse:
        return respond(
            exc.http_status, exc.code, exc.message, details=exc.details, retryable=exc.retryable
        )

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only; never echo input values back.
        fields = [
            {"loc": [str(p) for p in err.get("loc", ())], "msg": str(err.get("msg", ""))}
            for err in exc.errors()
        ]
        return respond(
            422, "VALIDATION_ERROR", "Request validation failed.", details={"fields": fields}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        return respond(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled.exception", extra={"fields": {"type": type(exc).__name__}})
        return respond(500, "SYSTEM_ERROR", "An unexpected error occurred.", retryable=False)
