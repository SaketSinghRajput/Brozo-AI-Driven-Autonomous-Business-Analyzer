"""Reason-coded ingestion errors (TRD section 35; fixture labels use `SECURITY_ERROR(ZIP_BOMB)`).

The envelope `error_code` stays the TRD class (`SECURITY_ERROR`, `DATA_ERROR`); the specific
reason travels in `details.reason` and as a `[REASON]` message prefix, so a failed job
(`JobError` has only code + message) still tells the user exactly why.
"""

from typing import Any

from app.core.errors import ApplicationError, DataError, SecurityError


class UploadTooLargeError(SecurityError):
    http_status = 413


class UnsupportedFormatError(DataError):
    http_status = 415


def reject[E: ApplicationError](
    cls: type[E], reason: str, message: str, *, hint: str | None = None, **details: Any
) -> E:
    payload: dict[str, Any] = {"reason": reason, **details}
    if hint:
        payload["hint"] = hint
    return cls(f"[{reason}] {message}", details=payload)


def reason_of(exc: ApplicationError) -> str:
    reason = (exc.details or {}).get("reason")
    return str(reason) if reason else exc.code
