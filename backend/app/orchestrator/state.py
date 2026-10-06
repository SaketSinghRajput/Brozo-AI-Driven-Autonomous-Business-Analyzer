"""TurnStateMachine, budgets and loop detector (TASK-AGENT-001, design §33).

One instance per chat turn. ``to()`` rejects transitions that design §33 does not allow and
checks the step, token and wall-clock budgets on every transition, so every path out of every
state is bounded. The transition history is persisted with the answer (``TurnRecord.trace``).
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.errors import ApplicationError
from app.orchestrator.domain import TurnLimits, TurnState, TurnUsage

S = TurnState
TERMINAL = frozenset(
    {S.NEEDS_CLARIFICATION, S.REFUSED, S.COMPLETED, S.REQUIRES_REVIEW, S.RECOVERED}
)
_ALLOWED: dict[TurnState, frozenset[TurnState]] = {
    S.IDLE: frozenset({S.CLASSIFYING}),
    S.CLASSIFYING: frozenset({S.NEEDS_CLARIFICATION, S.REFUSED, S.PLANNING}),
    S.PLANNING: frozenset({S.EXECUTING, S.REFUSED, S.NEEDS_CLARIFICATION}),
    S.EXECUTING: frozenset({S.WAITING_FOR_TOOL, S.COMPOSING, S.REFUSED}),  # all steps failed
    S.WAITING_FOR_TOOL: frozenset({S.EXECUTING}),
    S.COMPOSING: frozenset({S.VALIDATING}),
    S.VALIDATING: frozenset({S.COMPLETED, S.RETRYING, S.REQUIRES_REVIEW}),
    S.RETRYING: frozenset({S.PLANNING, S.COMPOSING}),
    S.FAILED: frozenset({S.RECOVERED}),
}


class TurnBudgetExceeded(ApplicationError):
    code, http_status = "BUDGET_EXCEEDED", 429


class IllegalTransition(ApplicationError):
    code = "STATE_CONFLICT"


class TurnStateMachine:
    def __init__(self, limits: TurnLimits, *, now: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self.state = S.IDLE
        self.history: list[TurnState] = [S.IDLE]
        self.retries = 0
        self.tokens = 0
        self.tool_calls = 0
        self._now = now
        self._deadline = now() + limits.wall_clock_timeout_s
        self._seen_calls: set[str] = set()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def to(self, target: TurnState) -> TurnState:
        allowed = _ALLOWED.get(self.state, frozenset())
        fatal = target is S.FAILED and not self.terminal and self.state is not S.FAILED
        if target not in allowed and not fatal:
            raise IllegalTransition(f"illegal turn transition {self.state} -> {target}")
        if target is S.RETRYING:
            if self.retries >= self.limits.max_retries:
                raise IllegalTransition("retry budget exhausted")
            self.retries += 1
        self.state = target
        self.history.append(target)
        if not fatal and target not in TERMINAL:
            self.check()
        return target

    def check(self) -> None:
        """Budget check; raises TurnBudgetExceeded. Called on transitions and before LLM calls."""
        if len(self.history) - 1 > self.limits.max_steps:
            raise TurnBudgetExceeded("turn step limit reached", retryable=False)
        if self.tokens > self.limits.max_llm_tokens:
            raise TurnBudgetExceeded("turn LLM token budget exhausted", retryable=False)
        if self.remaining_s() <= 0:
            raise TurnBudgetExceeded("turn wall-clock limit reached", retryable=True)

    def remaining_s(self) -> float:
        return self._deadline - self._now()

    def charge_tokens(self, tokens: int) -> None:
        self.tokens += tokens
        self.check()

    async def bounded[T](self, awaitable: Awaitable[T]) -> T:
        """Await within the remaining wall-clock budget (an LLM or tool call cannot outlive it)."""
        try:
            async with asyncio.timeout(max(self.remaining_s(), 0.0)):
                return await awaitable
        except TimeoutError:
            raise TurnBudgetExceeded("turn wall-clock limit reached", retryable=True) from None

    def can_call_tool(self) -> bool:
        return self.tool_calls < self.limits.max_tool_calls

    def record_call(self, name: str, args: dict[str, Any]) -> bool:
        """Loop detector: False when the identical (tool, args) was already issued this turn."""
        canonical = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
        key = f"{name}\x1f{hashlib.sha256(canonical.encode()).hexdigest()}"
        if key in self._seen_calls:
            return False
        self._seen_calls.add(key)
        self.tool_calls += 1
        return True

    def usage(self) -> TurnUsage:
        return TurnUsage(
            steps=len(self.history) - 1,
            tool_calls=self.tool_calls,
            llm_tokens=self.tokens,
            retries=self.retries,
        )
