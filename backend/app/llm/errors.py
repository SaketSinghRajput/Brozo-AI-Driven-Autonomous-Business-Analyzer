"""Typed LLM failures (design.md sections 29-32).

`LLMFailure` extends the core `LLMError` (code LLM_ERROR, HTTP 503) with a machine-readable
reason, so callers can branch on *why* the call failed without parsing messages. `retryable`
marks transient failures: the gateway retries only those.
"""

from enum import StrEnum

from app.core.errors import LLMError


class LLMFailureReason(StrEnum):
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"
    UNSUPPORTED = "UNSUPPORTED"


TRANSIENT_REASONS = frozenset(
    {
        LLMFailureReason.TIMEOUT,
        LLMFailureReason.UNAVAILABLE,
        LLMFailureReason.PROVIDER_ERROR,
    }
)


class LLMFailure(LLMError):
    def __init__(
        self, reason: LLMFailureReason, message: str, *, retryable: bool | None = None
    ) -> None:
        super().__init__(
            message,
            details={"reason": reason.value},
            retryable=reason in TRANSIENT_REASONS if retryable is None else retryable,
        )
        self.reason = reason
