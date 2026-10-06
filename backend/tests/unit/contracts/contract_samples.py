"""One valid JSON payload per contract, shared by the round-trip and rejection tests."""

import copy
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.dashboard.domain import ChartSpec, DashboardSpec
from app.data.domain import Dataset, DatasetVersion, FileFacts, ParsedBatch
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
from app.orchestrator.domain import TurnEvent
from app.planner.domain import AnalysisIntent, AnalysisPlan, CapabilityReport, CapabilityRuleSet
from app.tools.domain import ToolContext, ToolMetadata, ToolResult, ToolSpecForLLM
from app.validator.domain import ValidationRequest, ValidationResult

Payload = dict[str, Any]
EXAMPLES = Path(__file__).parent / "examples"
H = "sha256:" + "a" * 64
T = "2026-09-28T10:00:00Z"


def example(name: str) -> Payload:
    data: Payload = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def changed(payload: Payload, path: str, value: Any) -> Payload:
    """Deep copy of ``payload`` with the dotted ``path`` (list indices allowed) set to ``value``."""
    out = copy.deepcopy(payload)
    *parents, last = path.split(".")
    node: Any = out
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return out


METRICS: Payload = {
    "primary": "mae",
    "values": {"mae": 1203.4, "rmse": 1610.2},
    "baseline": {"name": "seasonal_naive", "values": {"mae": 1590.0}},
}
MODEL: Payload = {"name": "ets", "library": "lib@1.0", "params": {"trend": "add", "sp": 12}}
INTENT: Payload = {
    "objective": "trend",
    "metrics": ["Revenue"],
    "filters": [{"dimension": "Region", "op": "in", "value": ["North", "South"]}],
    "time": {"grain": "month", "start": "2026-01-01", "end": "2026-08-31"},
    "source": "user_question",
}
UNAVAILABLE: Payload = {
    "analysis": "churn_analysis",
    "reason_code": "NO_RETENTION_HISTORY",
    "message": "No customer retention history",
    "would_enable": "subscription start/end dates per customer",
}

SAMPLES: dict[type[BaseModel], Payload] = {
    Dataset: {
        "id": "ds_000001",
        "workspace_id": "ws_000001",
        "name": "sales",
        "source_type": "file_upload",
        "created_at": T,
    },
    DatasetVersion: {
        "id": "dsv_000001",
        "dataset_id": "ds_000001",
        "workspace_id": "ws_000001",
        "version_no": 1,
        "source_sha256": H,
        "content_hash": H,
        "storage_uri": "file://datastore/dsv_000001.parquet",
        "row_count": 100,
        "state": "PROFILED",
        "state_changed_at": T,
        "review_flags": ["DOMAIN_UNCERTAIN"],
        "created_at": T,
    },
    FileFacts: {
        "path": "uploads/sales.csv",
        "size_bytes": 2048,
        "detected_type": "csv",
        "encoding": "utf-8",
        "bom": False,
        "container": None,
    },
    ParsedBatch: {
        "columns": ["order_id", "amount"],
        "rows": [["o1", 10.5], ["o2", None]],
        "rejected_rows": [{"row_number": 3, "reason": "wrong field count"}],
        "total_rows_seen": 3,
    },
    ProfileResult: {
        "dataset_version_id": "dsv_000001",
        "engine_version": "0.1.0",
        "row_count": 4,
        "duplicate_rows": 0,
        "columns": [
            {
                "name": "amount",
                "physical_type": "float",
                "parse_success_ratio": 1.0,
                "null_ratio": 0.25,
                "unique_ratio": 1.0,
                "cardinality": 3,
                "numeric": {
                    "min": 1.0,
                    "max": 3.0,
                    "mean": 2.0,
                    "median": 2.0,
                    "std": 1.0,
                    "var": 1.0,
                    "skew": 0.0,
                    "quantiles": {"q50": 2.0},
                    "zero_ratio": 0.0,
                    "neg_ratio": 0.0,
                },
                "outliers": {"method": "iqr", "count": 0, "sample_indices": []},
            },
            {
                "name": "order_date",
                "physical_type": "date",
                "parse_success_ratio": 1.0,
                "null_ratio": 0.0,
                "unique_ratio": 1.0,
                "cardinality": 4,
                "temporal": {
                    "min": "2026-01-01T00:00:00Z",
                    "max": "2026-01-04T00:00:00Z",
                    "granularity": "day",
                    "regularity": 1.0,
                    "gaps": 0,
                    "tz_mixed": False,
                },
            },
        ],
        "correlations": [
            {"a": "amount", "b": "order_date", "method": "spearman", "coefficient": 0.5, "n": 3}
        ],
        "sampled": False,
        "sample_size": None,
    },
    Understanding: {
        "dataset_version_id": "dsv_000001",
        "engine_version": "0.1.0",
        "columns": [
            {
                "name": "customer_id",
                "source_name": "Customer ID",
                "physical_type": "string",
                "semantic_type": "identifier",
                "confidence": 0.95,
                "signals": ["unique_ratio=1.0"],
            },
            {
                "name": "net_sales",
                "source_name": "Net Sales",
                "physical_type": "float",
                "semantic_type": "currency_amount",
                "confidence": 0.9,
                "signals": ["name:sales"],
            },
            {
                "name": "order_date",
                "source_name": "Order Date",
                "physical_type": "date",
                "semantic_type": "datetime",
                "confidence": 0.99,
                "signals": ["parse_ratio=1.0"],
            },
        ],
        "entities": [{"name": "Customer", "key_columns": ["customer_id"], "confidence": 0.9}],
        "relationships": [
            {
                "from": "orders.customer_id",
                "to": "customers.customer_id",
                "cardinality": "many_to_one",
                "inclusion": 0.98,
                "confidence": 0.8,
            }
        ],
        "time_dimensions": [
            {"column": "order_date", "grain": "day", "regularity": 0.9, "confidence": 0.9}
        ],
        "measures": [
            {
                "column": "net_sales",
                "kind": "amount",
                "additivity": "additive",
                "unit": "currency:INR",
            }
        ],
        "dimensions": [{"column": "customer_id", "type": "identifier", "cardinality_band": "high"}],
        "review_flags": [],
    },
    DomainClassification: {
        "status": "KNOWN",
        "primary": "retail",
        "candidates": [
            {
                "domain": "retail",
                "score": 0.82,
                "evidence": [{"kind": "lexicon", "detail": "columns sales, sku", "weight": 0.6}],
            },
            {"domain": "saas", "score": 0.2, "evidence": []},
        ],
        "confidence": 0.8,
        "pack_versions": {"retail": "1.2.0", "saas": "1.0.0"},
    },
    SemanticModel: {
        "semantic_model_id": "sm_000001",
        "dataset_version_id": "dsv_000001",
        "version": 3,
        "entities": [
            {"name": "Customer", "key": "customer_id", "source_table": "orders", "confidence": 0.94}
        ],
        "dimensions": [
            {"name": "Region", "column": "region", "type": "category", "entity": "Customer"},
            {"name": "Order Date", "column": "order_date", "type": "time", "grain": "day"},
        ],
        "measures": [
            {
                "name": "Sales Amount",
                "column": "net_sales",
                "unit": "currency:INR",
                "additivity": "additive",
                "default_agg": "sum",
                "confidence": 0.88,
                "aliases": ["revenue", "total_amt"],
            }
        ],
        "metrics": [
            {
                "name": "Revenue",
                "expr": "sum(measure('Sales Amount'))",
                "unit": "currency:INR",
                "direction": "higher_is_better",
                "source": "pack:retail@1.2",
            }
        ],
        "rules": [
            {"name": "active_customer", "expr": "days_since(max(dimension('Order Date'))) <= 90"}
        ],
        "graph": {
            "nodes": [
                {"id": "e1", "type": "entity", "name": "Customer"},
                {"id": "m1", "type": "measure", "name": "Sales Amount"},
            ],
            "edges": [
                {
                    "from": "m1",
                    "to": "e1",
                    "type": "measured_on",
                    "confidence": 0.9,
                    "evidence": "fk",
                }
            ],
        },
        "overrides": [{"field": "mapping.net_amt", "value": "Revenue", "by": "user_42", "at": T}],
    },
    CapabilityRuleSet: {
        "rules_version": "0.1.0",
        "analyses": {
            "forecast": {
                "requires": [
                    {"check": "time_dimension", "reason_code": "NO_TIME_DIMENSION"},
                    {
                        "check": "time_regularity",
                        "params": {"min": 0.8},
                        "reason_code": "IRREGULAR_TIME",
                    },
                    {
                        "check": "measure",
                        "params": {"additivity_in": ["additive", "semi_additive"]},
                        "reason_code": "NO_MEASURE",
                    },
                    {
                        "check": "history_periods",
                        "params": {"min": "config.forecast.min_periods"},
                        "reason_code": "INSUFFICIENT_HISTORY",
                    },
                ]
            },
            "churn_analysis": {
                "requires": [
                    {
                        "check": "entity",
                        "params": {"name": "Customer"},
                        "reason_code": "NO_CUSTOMER_IDENTIFIER",
                    },
                    {
                        "check": "any_of",
                        "reason_code": "NO_RETENTION_HISTORY",
                        "any_of": [
                            {"check": "subscription_status"},
                            {"check": "activity_history_per_customer"},
                        ],
                    },
                ]
            },
        },
    },
    CapabilityReport: {
        "dataset_version_id": "dsv_000001",
        "semantic_model_version": 3,
        "rules_version": "0.1.0",
        "available": ["kpi", "trend"],
        "unavailable": [UNAVAILABLE],
    },
    AnalysisIntent: INTENT,
    AnalysisPlan: {
        "plan_id": "pl_000001",
        "version": 1,
        "dataset_version_id": "dsv_000001",
        "semantic_model_version": 3,
        "intent": INTENT,
        "steps": [
            {
                "id": "S1",
                "type": "metric_timeseries",
                "tool": "analytics.metric_query",
                "args": {"metric": "Revenue", "grain": "month"},
                "expected_output": "ResultTable",
                "validation": ["schema"],
            },
            {
                "id": "S2",
                "type": "growth_rate",
                "tool": "analytics.growth",
                "depends_on": ["S1"],
                "expected_output": "GrowthResult",
                "validation": ["numeric"],
            },
            {
                "id": "S3",
                "type": "visualize",
                "tool": "viz.spec",
                "args": {"widget_type": "line_chart"},
                "depends_on": ["S1", "S2"],
                "expected_output": "ChartSpec",
            },
        ],
        "unavailable": [UNAVAILABLE],
    },
    TaskSpec: {
        "task": "forecast",
        "dataset_version_id": "dsv_000001",
        "metric": "Revenue",
        "time_column": "order_date",
        "grain": "month",
        "horizon": 3,
        "seed": 42,
    },
    ModelResult: {
        "model_run_id": "mr_000001",
        "task": "forecast",
        "model": MODEL,
        "inputs": {
            "dataset_version_id": "dsv_000001",
            "metric": "Revenue",
            "grain": "month",
            "rows": 36,
        },
        "training": {"split": "rolling_origin", "folds": 3, "horizon": 3, "seed": 42},
        "metrics": METRICS,
        "result_ref": "artifact://results/mr_000001/forecast.parquet",
        "limitations": ["Only 3 seasonal cycles of history"],
        "evidence_id": "ev_000001",
    },
    ModelRun: {
        "id": "mr_000001",
        "workspace_id": "ws_000001",
        "task": "forecast",
        "params": {"sp": 12},
        "selection": {
            "best": "ets",
            "primary_metric": "mae",
            "scores": {"seasonal_naive": 1590.0, "ets": 1203.4},
            "excluded": [{"candidate": "arima", "reason": "over budget"}],
            "margin_rule": "relative 5% and consistent across folds",
            "fell_back_to_baseline": False,
        },
        "metrics": METRICS,
        "seed": 42,
        "evidence_id": "ev_000001",
        "created_at": T,
    },
    ForecastResult: {
        "metric": "Revenue",
        "grain": "month",
        "horizon": 2,
        "points": [
            {"period": "2026-09-01", "value": 10.0, "lower": 8.0, "upper": 12.0},
            {"period": "2026-10-01", "value": 11.0, "lower": 8.5, "upper": 13.5},
        ],
        "interval_level": 0.8,
        "model": MODEL,
        "backtest": METRICS,
        "assumptions": ["Seasonality is stable"],
    },
    AnomalyResult: {
        "metric": "Revenue",
        "anomalies": [
            {
                "subject": {"date": "2026-08-14", "region": "North"},
                "metric": "Revenue",
                "observed": 950.0,
                "expected": 400.0,
                "score": 0.93,
                "direction": "high",
            }
        ],
        "explanation": {"method": "robust_z", "threshold": 3.5, "caveats": [ANOMALY_CAVEAT]},
    },
    Evidence: {
        "evidence_id": "ev_000001",
        "workspace_id": "ws_000001",
        "kind": "tool_result",
        "dataset_id": "ds_000001",
        "dataset_version_id": "dsv_000001",
        "dataset_content_hash": H,
        "semantic_model_version": 3,
        "operation": "analytics.metric_query",
        "tool_version": "1.2.0",
        "arguments": {"metric": "Revenue", "grain": "month", "range": ["2026-07-01", "2026-08-31"]},
        "query": {"engine": "engine@1.0", "sql": "SELECT 1", "params": {}},
        "calculation": "pct_change(Revenue[2026-08], Revenue[2026-07])",
        "result": {"value": -0.142, "unit": "pct", "rows": 2, "result_hash": H},
        "confidence": {"level": "high", "score": 0.91, "basis": ["exact_recompute_match"]},
        "created_at": T,
        "software_version": "0.1.0+abc1234",
    },
    Claim: {
        "claim_id": "cl_000001",
        "workspace_id": "ws_000001",
        "kind": "answer_sentence",
        "text": "Revenue fell 14.2% in August.",
        "evidence_ids": ["ev_000001"],
        "validation_id": "va_000001",
        "created_at": T,
    },
    ValidationRequest: {
        "subject_type": "answer",
        "subject_id": "ans_000001",
        "segments": [
            {"type": "text", "text": "Revenue fell "},
            {
                "type": "number",
                "evidence_id": "ev_000001",
                "field": "result.value",
                "format": "pct:1",
            },
            {"type": "caveat", "text": "Two months only."},
        ],
        "evidence_ids": ["ev_000001"],
        "semantic_model_version": 3,
        "impact": "normal",
    },
    ValidationResult: {
        "id": "va_000001",
        "subject_type": "answer",
        "subject_id": "ans_000001",
        "status": "UNCERTAIN",
        "issues": [
            {"code": "ENTAILMENT_UNSURE", "severity": "soft", "target": "seg_2", "detail": "x"}
        ],
        "confidence": {"level": "medium", "score": 0.6, "basis": ["soft_issue"]},
        "validator_versions": {"numeric": "0.1.0"},
    },
    ChartSpec: example("dashboard_retail")["widgets"][2]["chart"],
    DashboardSpec: example("dashboard_retail"),
    ToolMetadata: {
        "name": "analytics.metric_query",
        "version": "1.2.0",
        "description": "Compute a semantic metric.",
        "permission": "SQL_READ",
        "side_effects": "none",
        "timeout_s": 10.0,
        "max_calls_per_turn": 5,
        "parallel_safe": True,
        "runs_in": "worker",
    },
    ToolResult: {
        "tool": "analytics.metric_query",
        "tool_version": "1.2.0",
        "status": "ok",
        "output": {"value": 1.5, "unit": "pct"},
        "evidence_id": "ev_000001",
        "duration_ms": 12,
    },
    ToolSpecForLLM: {
        "name": "analytics.growth",
        "description": "Growth between periods.",
        "input_schema": {"type": "object", "properties": {"metric": {"type": "string"}}},
    },
    ToolContext: {
        "workspace_id": "ws_000001",
        "principal_id": "usr_000001",
        "trace_id": "4f1c2a",
        "permissions": ["SQL_READ", "MODEL_RUN"],
        "dataset_version_id": "dsv_000001",
    },
    TurnEvent: {
        "seq": 3,
        "event": {
            "type": "segment",
            "index": 1,
            "segment": {
                "type": "number",
                "evidence_id": "ev_000001",
                "field": "result.value",
                "format": "currency:INR:2",
                "value": 540.0,
                "unit": "currency:INR",
            },
        },
    },
}
