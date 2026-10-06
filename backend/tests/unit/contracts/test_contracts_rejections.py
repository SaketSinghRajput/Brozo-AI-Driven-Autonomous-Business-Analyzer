"""At least one rejection per validator: a valid sample with one field broken must fail."""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.dashboard.domain import ChartSpec, DashboardSpec
from app.data.domain import DatasetVersion, ParsedBatch
from app.data.profiling.domain import ProfileResult
from app.data.semantic.domain import SemanticModel
from app.data.understanding.domain import DomainClassification, Understanding
from app.evidence.domain import Claim, Evidence
from app.ml.domain import (
    ANOMALY_CAVEAT,
    AnomalyResult,
    ForecastResult,
    ModelResult,
    ModelRun,
    TaskSpec,
)
from app.planner.domain import AnalysisIntent, AnalysisPlan, CapabilityReport, CapabilityRuleSet
from app.tools.domain import ToolMetadata, ToolResult
from app.validator.domain import ValidationResult

from .contract_samples import SAMPLES, changed

SOFT = {"code": "X", "severity": "soft", "target": "t", "detail": "d"}
HARD = {**SOFT, "severity": "hard"}
FORECAST_RULE = "analyses.forecast.requires"
CHURN_ANY_OF = "analyses.churn_analysis.requires.1"

CASES: list[tuple[type[BaseModel], str, Any, str]] = [
    # data
    (DatasetVersion, "state", "VALIDATION_FAILED", "failure must be set"),
    (DatasetVersion, "failure", {"code": "X", "message": "m"}, "failure must be set"),
    (DatasetVersion, "content_hash", None, "requires content_hash"),
    (DatasetVersion, "source_sha256", "abc", "pattern"),
    (DatasetVersion, "id", "ds_1", "pattern"),
    (ParsedBatch, "rows.0", ["only-one"], "one value per column"),
    (ParsedBatch, "total_rows_seen", 1, "total_rows_seen"),
    # profiling
    (ProfileResult, "columns.0.null_ratio", 1.5, "less than or equal to 1"),
    (ProfileResult, "columns.1.name", "amount", "unique"),
    (ProfileResult, "duplicate_rows", 9, "duplicate_rows"),
    (ProfileResult, "columns.0.cardinality", 4, "cardinality exceeds"),
    (ProfileResult, "sampled", True, "sample_size"),
    (ProfileResult, "correlations.0.b", "missing", "profiled columns"),
    # understanding + domain
    (Understanding, "measures.0.column", "ghost", "unknown columns"),
    (Understanding, "columns.1.name", "customer_id", "unique"),
    (Understanding, "columns.0.semantic_type", "banana", "semantic_type"),
    (DomainClassification, "candidates.1.score", 0.9, "sorted"),
    (DomainClassification, "status", "UNKNOWN", "UNKNOWN has no primary"),
    (DomainClassification, "primary", None, "KNOWN requires"),
    (DomainClassification, "primary", "fintech", "one of the candidates"),
    (DomainClassification, "status", "known", "status"),
    # semantic
    (SemanticModel, "dimensions.1.name", "Region", "dimensions names must be unique"),
    (SemanticModel, "dimensions.1.grain", None, "grain is required"),
    (SemanticModel, "measures.0.additivity", "non_additive", "cannot default to sum"),
    (SemanticModel, "graph.edges.0.to", "nowhere", "existing nodes"),
    (SemanticModel, "graph.nodes.1.id", "e1", "node ids must be unique"),
    # capability rules / report / intent / plan
    (CapabilityRuleSet, f"{FORECAST_RULE}.1.params", {"regularity": 0.8}, "unknown params"),
    (CapabilityRuleSet, f"{FORECAST_RULE}.0.reason_code", None, "needs a reason_code"),
    (CapabilityRuleSet, f"{FORECAST_RULE}.0.reason_code", "NOPE", "reason_code"),
    (CapabilityRuleSet, f"{FORECAST_RULE}.0.check", "vibes", "check"),
    (CapabilityRuleSet, f"{CHURN_ANY_OF}.any_of", [], "any_of children are required"),
    (CapabilityRuleSet, f"{CHURN_ANY_OF}.any_of.0.reason_code", "NO_TARGET", "must not carry"),
    (CapabilityRuleSet, "analyses.forecast.requires", [], "at least 1"),
    (CapabilityRuleSet, "rules_version", "v1", "pattern"),
    (CapabilityReport, "available", ["churn_analysis"], "both available and unavailable"),
    (AnalysisIntent, "filters.0.value", "North", "non-empty list"),
    (AnalysisIntent, "filters.0", {"dimension": "d", "op": "between", "value": [1]}, "exactly two"),
    (AnalysisIntent, "filters.0.op", "eq", "single value"),
    (AnalysisIntent, "time.start", "2026-12-01", "start must not be after end"),
    (AnalysisIntent, "objective", "vibes", "objective"),
    (AnalysisPlan, "steps.1.id", "S1", "unique"),
    (AnalysisPlan, "steps.1.depends_on", ["S9"], "unknown dependencies"),
    (AnalysisPlan, "steps.0.depends_on", ["S3"], "cycle"),
    (AnalysisPlan, "steps.0.depends_on", ["S1"], "cycle"),
    (AnalysisPlan, "steps.0.tool", "DROP TABLE", "pattern"),
    # ml
    (TaskSpec, "horizon", None, "forecast needs"),
    (TaskSpec, "task", "regression", "needs a target"),
    (ModelResult, "metrics.primary", "r2", "primary metric"),
    (ModelResult, "metrics.baseline.values", {"rmse": 1.0}, "baseline must report"),
    (ModelRun, "selection.best", "prophet", "scored candidates"),
    (ForecastResult, "points.0.value", 99.0, "within"),
    (ForecastResult, "points.0.upper", None, "come together"),
    (ForecastResult, "horizon", 3, "one point per horizon"),
    (ForecastResult, "interval_level", None, "interval_level is set exactly"),
    (AnomalyResult, "explanation.caveats", [], ANOMALY_CAVEAT),
    (AnomalyResult, "anomalies.0.score", 2.0, "less than or equal to 1"),
    # evidence / claim
    (Evidence, "dataset_content_hash", None, "go together"),
    (Evidence, "result.result_hash", "md5:abc", "pattern"),
    (Evidence, "kind", "gossip", "kind"),
    (Claim, "evidence_ids", [], "at least 1"),
    (Claim, "widget_id", "w1", "answer_sentence claims"),
    (Claim, "kind", "widget", "widget claims"),
    # validation
    (ValidationResult, "status", "VALID", "VALID results carry no issues"),
    (ValidationResult, "issues", [HARD], "hard issue requires"),
    (ValidationResult, "issues", [], "UNCERTAIN requires"),
    (ValidationResult, "status", "PASS", "status"),
    # dashboard
    (ChartSpec, "evidence_id", None, "evidence_id"),
    (ChartSpec, "x", None, "need an x field"),
    (ChartSpec, "type", "kpi", "kpi charts"),
    (ChartSpec, "y", [], "at least 1"),
    (DashboardSpec, "widgets.2.chart.type", "bar", "cannot render chart"),
    (DashboardSpec, "widgets.4.reason_code", None, "reason_code"),
    (DashboardSpec, "widgets.4.reason_code", "BECAUSE", "reason_code"),
    (DashboardSpec, "widgets.0.value_ref", None, "value_ref"),
    (DashboardSpec, "widgets.1.id", "w1", "widget ids must be unique"),
    (DashboardSpec, "layout.items.0.id", "w9", "layout items must reference"),
    (DashboardSpec, "layout.items.0.w", 13, "fit within"),
    (DashboardSpec, "widgets.0.widget_type", "gauge", "widget_type"),
    # tools
    (ToolMetadata, "permission", "sql_read", "permission"),
    (ToolMetadata, "name", "metric_query", "pattern"),
    (ToolResult, "status", "error", "exactly its own payload"),
]


@pytest.mark.parametrize(
    ("model", "path", "value", "message"),
    CASES,
    ids=[f"{m.__name__}:{p}={v!r}" for m, p, v, _ in CASES],
)
def test_rejects(model: type[BaseModel], path: str, value: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        model.model_validate(changed(SAMPLES[model], path, value))


def test_error_result_cannot_carry_evidence() -> None:
    payload = {
        **SAMPLES[ToolResult],
        "status": "error",
        "output": None,
        "error": {"code": "TOOL_ERROR", "message": "timeout", "retryable": False},
    }
    with pytest.raises(ValidationError, match="only ok results"):
        ToolResult.model_validate(payload)


def test_tool_result_evidence_without_dataset_is_rejected() -> None:
    payload = {
        **SAMPLES[Evidence],
        "dataset_id": None,
        "dataset_version_id": None,
        "dataset_content_hash": None,
    }
    with pytest.raises(ValidationError, match="must reference a dataset version"):
        Evidence.model_validate(payload)
    Evidence.model_validate({**payload, "kind": "citation"})  # citations need no dataset
