#!/usr/bin/env python
"""Export the JSON Schema of every top-level backend contract to shared/schemas/.

Run (from backend/, so the backend environment is used):
    uv run python ../scripts/export_schemas.py           # regenerate
    uv run python ../scripts/export_schemas.py --check   # exit 1 if shared/schemas is stale

Output is deterministic: one <name>.schema.json per contract, sorted keys, 2-space indent,
LF line endings. Stale *.schema.json files for removed contracts are deleted.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "shared" / "schemas"
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import BaseModel  # noqa: E402

from app.dashboard.domain import ChartSpec, DashboardSpec  # noqa: E402
from app.data.domain import Dataset, DatasetVersion, FileFacts  # noqa: E402
from app.data.profiling.domain import ProfileResult  # noqa: E402
from app.data.semantic.domain import SemanticModel  # noqa: E402
from app.data.understanding.domain import DomainClassification, Understanding  # noqa: E402
from app.evidence.domain import Claim, Evidence  # noqa: E402
from app.ml.domain import (  # noqa: E402
    AnomalyResult,
    ForecastResult,
    ModelResult,
    ModelRun,
    TaskSpec,
)
from app.orchestrator.domain import TurnEvent  # noqa: E402
from app.planner.domain import (  # noqa: E402
    AnalysisIntent,
    AnalysisPlan,
    CapabilityReport,
    CapabilityRuleSet,
)
from app.tools.domain import ToolMetadata, ToolResult, ToolSpecForLLM  # noqa: E402
from app.validator.domain import ValidationRequest, ValidationResult  # noqa: E402

CONTRACTS: dict[str, type[BaseModel]] = {
    "analysis_intent": AnalysisIntent,
    "analysis_plan": AnalysisPlan,
    "anomaly_result": AnomalyResult,
    "capability_report": CapabilityReport,
    "capability_rule_set": CapabilityRuleSet,
    "chart_spec": ChartSpec,
    "claim": Claim,
    "dashboard_spec": DashboardSpec,
    "dataset": Dataset,
    "dataset_version": DatasetVersion,
    "domain_classification": DomainClassification,
    "evidence": Evidence,
    "file_facts": FileFacts,
    "forecast_result": ForecastResult,
    "model_result": ModelResult,
    "model_run": ModelRun,
    "profile_result": ProfileResult,
    "semantic_model": SemanticModel,
    "task_spec": TaskSpec,
    "tool_metadata": ToolMetadata,
    "tool_result": ToolResult,
    "tool_spec_for_llm": ToolSpecForLLM,
    "turn_event": TurnEvent,
    "understanding": Understanding,
    "validation_request": ValidationRequest,
    "validation_result": ValidationResult,
}


def render() -> dict[str, str]:
    """File name -> exact file content for every contract."""
    return {
        f"{name}.schema.json": json.dumps(
            model.model_json_schema(), indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n"
        for name, model in sorted(CONTRACTS.items())
    }


def stale(out: Path = OUT) -> list[str]:
    """Names of schema files that are missing, different, or no longer generated."""
    expected = render()
    existing = {p.name for p in out.glob("*.schema.json")}
    changed = [
        name
        for name, text in expected.items()
        if name not in existing or (out / name).read_text(encoding="utf-8") != text
    ]
    return sorted(changed + list(existing - expected.keys()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export contract JSON Schemas.")
    parser.add_argument("--check", action="store_true", help="fail if files are out of date")
    args = parser.parse_args(argv)
    if args.check:
        outdated = stale()
        for name in outdated:
            print(f"out of date: shared/schemas/{name}")
        return 1 if outdated else 0
    OUT.mkdir(parents=True, exist_ok=True)
    expected = render()
    for old in OUT.glob("*.schema.json"):
        if old.name not in expected:
            old.unlink()
    for name, text in expected.items():
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(expected)} schemas to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
