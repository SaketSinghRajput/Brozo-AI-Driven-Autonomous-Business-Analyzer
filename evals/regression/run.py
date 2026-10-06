"""Regression golden outputs (TASK-TEST-008).

    cd backend && uv run python ../evals/regression/run.py            # check (exit 1 on change)
    cd backend && uv run python ../evals/regression/run.py --write    # accept the current output

For every fixture table (evals/pipeline.py TABLES) the production path is run without a database
and its outputs are snapshotted to evals/golden/regression/<fixture>.json:
- understanding summary: domain classification, semantic model metrics/measures/dimensions;
- the capability report (CapabilityEvaluator, available + unavailable with reason codes);
- the default plan (DefaultPlanner over the real analytics tools, deferred objectives included);
- key metric results: every semantic metric's ungrouped total via ``analytics.metric_query``
  through the real ToolExecutor (value rounded to 6 dp: DuckDB's parallel SUM order moves the
  last float bits).
An unintended change in any of these fails CI (backend/tests/unit/evals/test_regression.py);
an intended one is accepted with ``--write`` and reviewed in the diff.
"""

import argparse
import asyncio
import difflib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "backend"))
sys.path.insert(0, str(HERE.parent))

from pipeline import ROOT, SCOPE, TABLES, Understood, rig, understand_fixture  # noqa: E402

from app.evidence.recorder import CallKey  # noqa: E402
from app.planner.capabilities import CapabilityEvaluator  # noqa: E402
from app.planner.default_planner import DefaultPlanner  # noqa: E402
from app.planner.facts import build_facts  # noqa: E402
from app.tools.domain import ToolContext  # noqa: E402
from app.tools.executor import TurnBudget  # noqa: E402
from app.tools.permissions import Role, TurnProfile, effective_permissions  # noqa: E402

OUT = ROOT / "evals" / "golden" / "regression"


async def _metric_totals(data: Understood) -> dict[str, Any]:
    r = rig(data, "regression")
    model = data.model
    ctx = ToolContext(
        workspace_id=SCOPE.workspace_id,
        principal_id=SCOPE.principal_id,
        trace_id="regression",
        permissions=effective_permissions(Role.ANALYST, TurnProfile.CHAT_DEFAULT),
        dataset_version_id=model.dataset_version_id,
        semantic_model_version=model.version,
    )
    totals: dict[str, Any] = {}
    for n, metric in enumerate(model.metrics):
        key = CallKey("regression", f"S{n}")
        result = await r.executor.run(
            "analytics.metric_query", {"metric": metric.name}, ctx, key, TurnBudget(64)
        )
        out = result.output or {}
        value = out.get("value")
        totals[metric.name] = {
            "status": result.status,
            "value": round(value, 6) if isinstance(value, int | float) else None,
            "unit": out.get("unit"),
            "error": result.error.code if result.error else None,
        }
    return totals


def snapshot(data: Understood) -> dict[str, Any]:
    facts = build_facts(data.understanding, data.model, data.profile)
    report = CapabilityEvaluator().evaluate(facts)
    registry = rig(data, "regression").orchestrator._registry
    planned = DefaultPlanner(registry).plan(facts, report)
    model = data.model
    return {
        "fixture_table": data.name,
        "domain": data.domain.model_dump(mode="json"),
        "semantic_model": {
            "metrics": {m.name: {"expr": m.expr, "unit": m.unit} for m in model.metrics},
            "measures": {m.name: [m.column, m.unit, m.default_agg] for m in model.measures},
            "dimensions": {d.name: [d.column, d.type] for d in model.dimensions},
        },
        "capability_report": report.model_dump(mode="json"),
        "default_plan": planned.plan.model_dump(mode="json"),
        "deferred": planned.deferred,
        "metric_totals": asyncio.run(_metric_totals(data)),
    }


def render() -> dict[str, str]:
    rendered = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name in TABLES:
            body = snapshot(understand_fixture(name, Path(tmp)))
            rendered[name] = json.dumps(body, indent=2, sort_keys=True) + "\n"
    return rendered


def check(rendered: dict[str, str]) -> list[str]:
    """Unified diffs (truncated) of every snapshot that changed."""
    problems = []
    for name, text in rendered.items():
        path = OUT / f"{name}.json"
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            diff = difflib.unified_diff(
                current.splitlines(), text.splitlines(), f"{name} (golden)", f"{name} (now)"
            )
            problems.append("\n".join(list(diff)[:60]))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Regression golden outputs per fixture.")
    parser.add_argument("--write", action="store_true", help="accept the current outputs")
    args = parser.parse_args()
    rendered = render()
    if args.write:
        OUT.mkdir(parents=True, exist_ok=True)
        for name, text in rendered.items():
            (OUT / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {len(rendered)} snapshots to {OUT.relative_to(ROOT)}")
        return 0
    problems = check(rendered)
    print("\n\n".join(problems) or f"{len(rendered)} snapshots unchanged")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
