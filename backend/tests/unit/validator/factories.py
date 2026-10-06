"""Synthetic evidence, semantic models and requests for validator tests (no real data)."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue

from app.core.ids import SequentialIds
from app.data.semantic.domain import SemanticModel
from app.evidence.domain import Confidence, Evidence, EvidenceResult, ModelRef
from app.validator.domain import (
    AnswerSegment,
    CaveatSegment,
    NumberSegment,
    TextSegment,
    ValidationRequest,
    ValidationResult,
)
from app.validator.pipeline import build_pipeline

WS = "ws_0000aa"
OTHER_WS = "ws_0000bb"
DSV = "dsv_000001"
HASH = "sha256:" + "ab" * 32
AT = datetime(2026, 9, 28, tzinfo=UTC)


def evidence(
    evidence_id: str = "ev_rev",
    *,
    value: float | None = -0.142,
    unit: str | None = "pct",
    rows: int | None = 2,
    workspace_id: str = WS,
    dataset_id: str = "ds_000001",
    dataset_version_id: str = DSV,
    semantic_model_version: int | None = 1,
    operation: str = "analytics.metric_query",
    arguments: dict[str, JsonValue] | None = None,
    model: bool = False,
    confidence: float | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        workspace_id=workspace_id,
        kind="tool_result",
        dataset_id=dataset_id,
        dataset_version_id=dataset_version_id,
        dataset_content_hash=HASH,
        semantic_model_version=semantic_model_version,
        operation=operation,
        tool_version="1.0.0",
        arguments={"metric": "Revenue"} if arguments is None else arguments,
        result=EvidenceResult(value=value, unit=unit, rows=rows, result_hash=HASH),
        model=ModelRef(model_id="m_1", model_version="1") if model else None,
        confidence=None
        if confidence is None
        else Confidence(level="medium", score=confidence, basis=["test"]),
        created_at=AT,
        software_version="0.1.0+test",
    )


def semantic_model(version: int = 1, dataset_version_id: str = DSV) -> SemanticModel:
    return SemanticModel.model_validate(
        {
            "semantic_model_id": "sm_000001",
            "dataset_version_id": dataset_version_id,
            "version": version,
            "entities": [
                {"name": "Order", "key": "order_id", "source_table": "orders", "confidence": 1}
            ],
            "dimensions": [
                {"name": "Region", "column": "region", "type": "category"},
                {"name": "Order Date", "column": "order_date", "type": "time", "grain": "day"},
            ],
            "measures": [
                {
                    "name": "Revenue",
                    "column": "amount",
                    "unit": "currency:INR",
                    "additivity": "additive",
                    "default_agg": "sum",
                    "confidence": 1,
                    "aliases": ["Sales"],
                }
            ],
            "metrics": [{"name": "Average Order Value", "expr": "Revenue / count(Order)"}],
        }
    )


def text(value: str) -> TextSegment:
    return TextSegment(text=value)


def caveat(value: str) -> CaveatSegment:
    return CaveatSegment(text=value)


def number(
    evidence_id: str = "ev_rev", fmt: str = "pct:1", field: str = "result.value"
) -> NumberSegment:
    return NumberSegment(evidence_id=evidence_id, field=field, format=fmt)


def request(
    *segments: AnswerSegment,
    evidence_ids: list[str] | None = None,
    impact: str = "normal",
    semantic_model_version: int | None = None,
) -> ValidationRequest:
    return ValidationRequest.model_validate(
        {
            "subject_type": "answer",
            "subject_id": "ans_1",
            "segments": [s.model_dump() for s in segments],
            "evidence_ids": ["ev_rev"] if evidence_ids is None else evidence_ids,
            "impact": impact,
            "semantic_model_version": semantic_model_version,
        }
    )


def bundle(*records: Evidence) -> dict[str, Evidence]:
    return {e.evidence_id: e for e in records}


def run(
    req: ValidationRequest,
    ev: Mapping[str, Evidence],
    models: Mapping[str, SemanticModel] | None = None,
    **kw: Any,
) -> ValidationResult:
    """Runs the full v1 pipeline for workspace ``WS``."""
    models = {DSV: semantic_model()} if models is None else models
    pipeline = build_pipeline(WS, models, SequentialIds())
    return asyncio.run(pipeline.run(req, ev, **kw))
