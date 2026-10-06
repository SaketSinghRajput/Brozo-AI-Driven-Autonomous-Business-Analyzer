"""EvidenceRecorder — the only component that writes evidence (TASK-EVID-002, design §39).

Evidence ids are deterministic in `(execution_id, step_id, attempt)`, so a handler or plan step
that re-runs after a crash re-derives the same id and the insert becomes a no-op.
Dataset references are read from `dataset_versions` (the system of record), never taken from
tool output, so evidence always points at the exact bytes the numbers came from.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.core.clock import Clock
from app.core.errors import NotFoundError
from app.core.scope import WorkspaceScope
from app.evidence.domain import Evidence, EvidenceResult, ModelRef, QueryInfo
from app.evidence.preview import rows_preview
from app.evidence.repository import EvidenceRepository
from app.ml.repository import ModelRunRepository
from app.tools.domain import ToolContext, ToolMetadata


@dataclass(frozen=True, slots=True)
class CallKey:
    """Identifies one tool invocation within a run or chat turn."""

    execution_id: str  # analysis run id or chat turn id
    step_id: str
    attempt: int = 1

    @property
    def evidence_id(self) -> str:
        raw = f"{self.execution_id}\x1f{self.step_id}\x1f{self.attempt}".encode()
        return f"ev_{hashlib.sha256(raw).hexdigest()[:32]}"


def result_hash(output: dict[str, Any]) -> str:
    canonical = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


class EvidenceRecorder:
    def __init__(self, clock: Clock, software_version: str = __version__) -> None:
        self._clock = clock
        self._software_version = software_version

    def record(
        self,
        session: Session,
        scope: WorkspaceScope,
        key: CallKey,
        meta: ToolMetadata,
        args: dict[str, Any],
        output: dict[str, Any],
        ctx: ToolContext,
    ) -> str:
        """Write tool-result evidence in the caller's transaction; returns its id."""
        dataset_id = content_hash = None
        if ctx.dataset_version_id is not None:
            row = session.execute(
                text(
                    "SELECT dataset_id, content_hash FROM dataset_versions "
                    "WHERE id = :id AND workspace_id = :ws"
                ),
                {"id": ctx.dataset_version_id, "ws": scope.workspace_id},
            ).one_or_none()
            if row is None:
                raise NotFoundError("dataset version not found")
            dataset_id, content_hash = row
        rows = output.get("rows")
        query = output.get("query")  # compiled SQL tools report {engine, sql, params} (TRD §16)
        calculation = output.get("calculation")
        runs = ModelRunRepository(session)
        run_id = output.get("model_run_id")
        model = None
        if isinstance(run_id, str):  # ML tools: link to the run they stored (GAPS-6)
            found = runs.model_ref(scope, run_id)
            if found is None:
                raise NotFoundError("model run not found")
            model = ModelRef(model_run_id=run_id, model_id=found[0], model_version=found[1])
        evidence = Evidence(
            evidence_id=key.evidence_id,
            workspace_id=scope.workspace_id,
            kind="tool_result",
            dataset_id=dataset_id,
            dataset_version_id=ctx.dataset_version_id,
            dataset_content_hash=content_hash,
            semantic_model_version=ctx.semantic_model_version,
            operation=meta.name,
            tool_version=meta.version,
            arguments=args,
            query=QueryInfo.model_validate(query) if isinstance(query, dict) else None,
            calculation=calculation if isinstance(calculation, str) else None,
            result=EvidenceResult(
                value=_number(output.get("value")),
                unit=output.get("unit") if isinstance(output.get("unit"), str) else None,
                rows=rows if isinstance(rows, int) and not isinstance(rows, bool) else None,
                result_hash=result_hash(output),
            ),
            model=model,
            created_at=self._clock.now(),
            software_version=self._software_version,
        )
        EvidenceRepository(session).insert(scope, evidence, rows_preview(output))
        if model is not None and model.model_run_id is not None:
            runs.link_evidence(scope, model.model_run_id, evidence.evidence_id)  # same txn
        return evidence.evidence_id
