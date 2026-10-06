"""Host allow-list and circuit breaker for the LLM gateway (TASK-LLM-003, design.md section 29).

`HostAllowList` is the runtime twin of `LLMConfig`'s startup validator: adapters check their
endpoint with it before opening any connection, so an adapter constructed outside config
(tests, scripts) cannot reach a non-allow-listed host either (CLAUDE.md section 20).
"""

import time
from collections.abc import Callable, Iterable
from typing import Literal
from urllib.parse import urlsplit

from app.llm.errors import LLMFailure, LLMFailureReason


class HostAllowList:
    def __init__(self, hosts: Iterable[str]) -> None:
        self._hosts = frozenset(h.lower() for h in hosts)

    def check(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or host not in self._hosts:
            raise LLMFailure(
                LLMFailureReason.HOST_NOT_ALLOWED,
                f"LLM endpoint host {host!r} is not allow-listed",
                retryable=False,
            )
        if parts.username or parts.password:
            raise LLMFailure(
                LLMFailureReason.HOST_NOT_ALLOWED,
                "LLM endpoint must not embed credentials",
                retryable=False,
            )


class CircuitBreaker:
    """closed → (threshold consecutive failures) → open → (reset_after_s) → half_open.

    In half_open exactly one trial call is let through: success closes the breaker, failure
    re-opens it for another reset period. Not thread-safe; it lives on one event loop.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_after_s: float = 30.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_after_s = reset_after_s
        self._now = now
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        if self._opened_at is None:
            return "closed"
        if self._now() - self._opened_at >= self._reset_after_s:
            return "half_open"
        return "open"

    def before_call(self) -> None:
        state = self.state
        if state == "closed":
            return
        if state == "half_open" and not self._trial_in_flight:
            self._trial_in_flight = True
            return
        raise LLMFailure(
            LLMFailureReason.CIRCUIT_OPEN,
            "local LLM temporarily unavailable (circuit open)",
            retryable=True,  # retryable for the client later, never retried by the gateway
        )

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        if self._trial_in_flight or self._failures >= self._threshold:
            self._opened_at = self._now()
            self._trial_in_flight = False
