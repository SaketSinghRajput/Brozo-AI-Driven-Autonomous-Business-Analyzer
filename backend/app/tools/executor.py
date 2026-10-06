"""ToolExecutor — the single choke point for every tool call (TASK-TOOL-004, design §19).

Guard chain: registry lookup → strict argument validation → permission → turn budget →
timeout → output validation → evidence (via EvidenceRecorder only) → tool_run record.
Every call, including refused and failed ones, leaves a `tool_runs` row. Tools never receive a
database session, so they cannot write evidence themselves.

Error mapping (TASK-TOOL-005): every failure becomes ``ToolResult.error`` with a ``retryable`` flag
that callers (the chat ToolLoop) obey. Deterministic failures (bad arguments, permission, budget,
unknown names, a tool rejecting its own derived inputs) are never retryable; transient ones
(timeout, database connection loss) are. Unexpected crashes are logged and not retryable.
"""

import asyncio
import json
import logging
import re
import time
from collections import Counter
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.errors import ApplicationError, AuthorizationError, RateLimitedError, ToolError
from app.core.ids import IdGenerator
from app.core.observability import TOOL_CALLS, span
from app.core.scope import WorkspaceScope
from app.evidence.recorder import CallKey, EvidenceRecorder
from app.infra.db import UnitOfWork
from app.infra.repository import Repository
from app.planner.domain import Unavailability
from app.tools.domain import ErrorInfo, ToolContext, ToolMetadata, ToolResult
from app.tools.registry import ToolRegistry

log = logging.getLogger("app.tools")
_TOOL_NAME = re.compile(r"^[a-z]+(\.[a-z_]+)+$")
UNKNOWN_TOOL = "unknown.tool"


class TurnBudget:
    """Per-turn tool-call budget: a total cap plus each tool's `max_calls_per_turn`."""

    def __init__(self, max_calls: int) -> None:
        self._max_calls = max_calls
        self._calls: Counter[str] = Counter()

    def consume(self, meta: ToolMetadata) -> None:
        if self._calls.total() >= self._max_calls:
            raise RateLimitedError("tool-call budget for this turn is exhausted", retryable=False)
        if self._calls[meta.name] >= meta.max_calls_per_turn:
            raise RateLimitedError(f"{meta.name} call limit reached", retryable=False)
        self._calls[meta.name] += 1


class ToolRunRepository(Repository):
    def append(
        self,
        scope: WorkspaceScope,
        run_id: str,
        trace_id: str,
        result: ToolResult,
        args: dict[str, Any],
    ) -> None:
        self._session.execute(
            text(
                "INSERT INTO tool_runs (id, workspace_id, trace_id, tool, tool_version, args, "
                "status, duration_ms, evidence_id, error_code) VALUES (:id, :ws, :trace, :tool, "
                ":version, CAST(:args AS jsonb), :status, :ms, :ev, :err)"
            ),
            {
                "id": run_id,
                "ws": scope.workspace_id,
                "trace": trace_id,
                "tool": result.tool,
                "version": result.tool_version,
                "args": json.dumps(args, default=str),
                "status": result.status,
                "ms": result.duration_ms,
                "ev": result.evidence_id,
                "err": result.error.code if result.error else None,
            },
        )


def _parse_args(model: type[BaseModel], raw_args: dict[str, Any]) -> BaseModel:
    """Strict JSON-mode validation: no silent coercion of LLM-supplied arguments."""
    try:
        return model.model_validate_json(json.dumps(raw_args), strict=True)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) or "<root>" for e in exc.errors())
        raise ToolError(f"invalid tool arguments: {fields}") from None
    except TypeError, ValueError:
        raise ToolError("invalid tool arguments: not JSON-serialisable") from None


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        uow: UnitOfWork,
        recorder: EvidenceRecorder,
        ids: IdGenerator,
    ) -> None:
        self._registry = registry
        self._uow = uow
        self._recorder = recorder
        self._ids = ids

    async def run(
        self,
        name: str,
        raw_args: dict[str, Any],
        ctx: ToolContext,
        key: CallKey,
        budget: TurnBudget,
    ) -> ToolResult:
        # Registered names only: an LLM-invented tool name must not become a span/metric label.
        try:
            label = self._registry.get(name).meta.name
        except ToolError:
            label = UNKNOWN_TOOL
        with span(f"tool.{label}", tool=label) as s:
            result = await self._run(name, raw_args, ctx, key, budget)
            s.set_attribute("tool.status", result.status)
        TOOL_CALLS.labels(label, result.status).inc()
        return result

    async def _run(
        self,
        name: str,
        raw_args: dict[str, Any],
        ctx: ToolContext,
        key: CallKey,
        budget: TurnBudget,
    ) -> ToolResult:
        started = time.perf_counter()
        meta: ToolMetadata | None = None
        args: dict[str, Any] = raw_args
        output: dict[str, Any] | None = None
        unavailable: Unavailability | None = None
        error: ErrorInfo | None = None
        try:
            tool = self._registry.get(name)
            meta = tool.meta
            parsed = _parse_args(tool.input_model, raw_args)
            args = parsed.model_dump(mode="json")
            if meta.permission not in ctx.permissions:
                raise AuthorizationError(f"{meta.name} requires {meta.permission}")
            budget.consume(meta)
            async with asyncio.timeout(meta.timeout_s):
                out = await tool(parsed, ctx)
            if isinstance(out, Unavailability):
                unavailable = out
            elif isinstance(out, tool.output_model):
                output = tool.output_model.model_validate(out.model_dump()).model_dump(mode="json")
            else:
                raise ToolError(f"{meta.name} returned an invalid output")
        except ApplicationError as exc:
            error = ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
        except TimeoutError:
            error = ErrorInfo(code="TOOL_TIMEOUT", message="tool timed out", retryable=True)
        except ValidationError:  # a tool built an invalid internal request from valid args
            message = "tool rejected its inputs"
            error = ErrorInfo(code="TOOL_ERROR", message=message, retryable=False)
        except OperationalError:
            log.warning("tool %s lost its database connection", name)
            error = ErrorInfo(code="DATABASE_ERROR", message="database unavailable", retryable=True)
        except Exception:
            log.exception("tool %s crashed", name)
            error = ErrorInfo(code="TOOL_ERROR", message="tool failed", retryable=False)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return await asyncio.to_thread(
            self._persist, name, meta, args, output, unavailable, error, ctx, key, elapsed_ms
        )

    def _persist(
        self,
        name: str,
        meta: ToolMetadata | None,
        args: dict[str, Any],
        output: dict[str, Any] | None,
        unavailable: Unavailability | None,
        error: ErrorInfo | None,
        ctx: ToolContext,
        key: CallKey,
        elapsed_ms: int,
    ) -> ToolResult:
        scope = WorkspaceScope(workspace_id=ctx.workspace_id, principal_id=ctx.principal_id)
        with self._uow(scope) as s:  # evidence and tool_run commit together (design §51)
            evidence_id = None
            if meta is not None and output is not None and meta.produces_evidence:
                try:
                    evidence_id = self._recorder.record(s, scope, key, meta, args, output, ctx)
                except ApplicationError as exc:
                    output = None
                    error = ErrorInfo(code=exc.code, message=exc.message, retryable=False)
            status = "error" if error else "unavailable" if unavailable else "ok"
            if meta is not None:
                tool_name, version = meta.name, meta.version
            else:
                tool_name = name if _TOOL_NAME.match(name) else UNKNOWN_TOOL
                version = "0.0.0"
            result = ToolResult(
                tool=tool_name,
                tool_version=version,
                status=status,
                output=output if status == "ok" else None,
                unavailable=unavailable if status == "unavailable" else None,
                error=error,
                evidence_id=evidence_id,
                duration_ms=elapsed_ms,
            )
            ToolRunRepository(s).append(scope, self._ids.new("tr"), ctx.trace_id, result, args)
        return result
