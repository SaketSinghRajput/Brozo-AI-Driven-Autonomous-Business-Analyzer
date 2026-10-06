"""Build the golden question sets (TASK-EVAL-002).

    cd backend && uv run python ../evals/golden/build.py            # (re)write jsonl + MANIFEST
    cd backend && uv run python ../evals/golden/build.py --check    # exit 1 if anything differs

For every item of spec.py: IntentOutput -> post_check (name resolution, refusal rules) ->
QuestionPlanner -> each plan step through the real ToolExecutor guard chain on the fixture table
understood through the production path. The expected tool names, arguments and numbers are
therefore computed by the deterministic analytics tools, never typed by hand. The build fails
when the deterministic outcome contradicts the hand-written expected outcome of an item that is
not marked ``known_gap`` (that is either a spec error or a product bug to look at).

Output: evals/golden/<dataset>.jsonl (one item per line, sorted keys) and MANIFEST.json with the
golden version, per-file SHA-256 and item counts, and the SHA-256 of spec.py and the fixtures.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from pipeline import FIXTURES, SCOPE, TABLES, Understood, rig, understand_fixture  # noqa: E402
from spec import ITEMS, Item  # noqa: E402

from app.core.ids import SequentialIds  # noqa: E402
from app.evidence.recorder import CallKey  # noqa: E402
from app.orchestrator.domain import Refusal  # noqa: E402
from app.orchestrator.intent import IntentOutput, post_check  # noqa: E402
from app.orchestrator.planner import Clarification, QuestionPlanner  # noqa: E402
from app.tools.domain import ToolContext  # noqa: E402
from app.tools.executor import TurnBudget  # noqa: E402
from app.tools.permissions import Role, TurnProfile, effective_permissions  # noqa: E402

GOLDEN_VERSION = "1.0.0"
MANIFEST = HERE / "MANIFEST.json"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _number(output: dict[str, Any] | None) -> tuple[float | None, str | None]:
    out = output or {}
    value = out.get("value")
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    unit = out.get("unit")
    # 6 decimals: DuckDB's parallel SUM order moves the last float bits between runs
    return (round(float(value), 6) if ok else None), (unit if isinstance(unit, str) else None)


async def _execute(item: Item, data: Understood) -> dict[str, Any]:
    """Deterministic expected outcome for one item (no LLM)."""
    model = data.model
    out = IntentOutput.model_validate(item["intent"])
    resolved = post_check(out, item["question"], model)
    expected: dict[str, Any] = {
        "route": out.route,
        "objective": out.objective,
        "intent": out.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        "tools": [],
        "tool_args": [],
        "numbers": [],
        "ranking_top": [],
        "caveats": resolved.caveats,
    }
    if resolved.intent is None:
        refusal = resolved.refusal
        return expected | {
            "outcome": "refused" if refusal else "clarify",
            "refusal_code": refusal.code if refusal else None,
        }
    expected["resolved_objective"] = resolved.intent.objective
    r = rig(data, item["question"])
    permissions = effective_permissions(Role.ANALYST, TurnProfile.CHAT_DEFAULT)
    plan = QuestionPlanner(r.orchestrator._registry, SequentialIds()).plan(
        resolved.intent, model, permissions
    )
    if isinstance(plan, Clarification):
        return expected | {"outcome": "clarify", "refusal_code": None}
    if isinstance(plan, Refusal):
        return expected | {"outcome": "refused", "refusal_code": plan.code}
    ctx = ToolContext(
        workspace_id=SCOPE.workspace_id,
        principal_id=SCOPE.principal_id,
        trace_id="golden",
        permissions=permissions,
        dataset_version_id=model.dataset_version_id,
        semantic_model_version=model.version,
    )
    budget = TurnBudget(16)
    any_ok = False
    for step in plan.steps:
        key = CallKey(f"golden-{item['id']}", step.id)
        result = await r.executor.run(step.tool, dict(step.args), ctx, key, budget)
        expected["tools"].append(step.tool)
        expected["tool_args"].append(step.args)
        any_ok = any_ok or result.status == "ok"
        value, unit = _number(result.output)
        if result.status == "ok" and value is not None and result.evidence_id:
            number = {"step": step.id, "tool": step.tool, "value": value, "unit": unit}
            expected["numbers"].append(number)
        if step.tool == "analytics.rank" and result.output is not None:
            expected["ranking_top"] = [i["key"] for i in result.output["ranking"]["items"][:3]]
        if result.status != "ok":
            code = result.error.code if result.error else "UNAVAILABLE"
            expected.setdefault("step_failures", []).append({"step": step.id, "code": code})
    if not any_ok:
        return expected | {"outcome": "refused", "refusal_code": "INSUFFICIENT_DATA"}
    return expected | {"outcome": "answered", "refusal_code": None}


def build() -> tuple[dict[str, str], list[str]]:
    """Return {dataset: jsonl text} and the list of spec/outcome contradictions."""
    problems: list[str] = []
    rows: dict[str, list[str]] = {name: [] for name in TABLES}
    with tempfile.TemporaryDirectory() as tmp:
        cache: dict[str, Understood] = {}
        for item in ITEMS:
            name = item["dataset"]
            if name not in cache:
                cache[name] = understand_fixture(name, Path(tmp))
            got = asyncio.run(_execute(item, cache[name]))
            outcome_ok = got["outcome"] == item["outcome"] and (
                item["outcome"] != "refused" or got["refusal_code"] in item["refusal_codes"]
            )
            if not outcome_ok and not item["known_gap"]:
                problems.append(
                    f"{item['id']}: spec says {item['outcome']} {item['refusal_codes']}, "
                    f"deterministic path gives {got['outcome']} {got['refusal_code']} "
                    f"{got.get('step_failures', '')}"
                )
            current = {"current_outcome": got.pop("outcome"), "current_refusal_code": None}
            current["current_refusal_code"] = got.pop("refusal_code")
            record = {
                "id": item["id"],
                "golden_version": GOLDEN_VERSION,
                "dataset": name,
                "table": TABLES[name],
                "stratum": item["stratum"],
                "question": item["question"],
                "injection": item["injection"],
                "answerable": item["outcome"] == "answered",
                "known_gap": item["known_gap"],
                "expected": {
                    **got,
                    **current,
                    "outcome": item["outcome"],
                    "refusal_codes": item["refusal_codes"],
                },
            }
            rows[name].append(json.dumps(record, sort_keys=True, ensure_ascii=False))
    return {name: "\n".join(lines) + "\n" for name, lines in rows.items() if lines}, problems


def manifest(files: dict[str, str]) -> str:
    fixtures = [FIXTURES / name / f"{table}.csv" for name, table in sorted(TABLES.items())]
    body = {
        "golden_version": GOLDEN_VERSION,
        "generator": "evals/golden/build.py",
        "spec_sha256": _sha((HERE / "spec.py").read_bytes()),
        "fixtures_sha256": {
            p.relative_to(FIXTURES).as_posix(): _sha(p.read_bytes()) for p in fixtures
        },
        "files": {
            f"{name}.jsonl": {"sha256": _sha(text.encode()), "items": text.count("\n")}
            for name, text in sorted(files.items())
        },
        "total_items": sum(text.count("\n") for text in files.values()),
    }
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the golden question sets.")
    parser.add_argument("--check", action="store_true", help="fail if the committed set differs")
    args = parser.parse_args()
    files, problems = build()
    for p in problems:
        print("CONTRADICTION", p)
    if problems:
        return 1
    outputs = {HERE / f"{name}.jsonl": text for name, text in files.items()}
    outputs[MANIFEST] = manifest(files)
    if args.check:
        stale = [
            p.name
            for p, text in outputs.items()
            if not p.exists() or p.read_text(encoding="utf-8") != text
        ]
        for name in stale:
            print("STALE", name)
        print("golden set", "differs" if stale else "up to date", f"({len(ITEMS)} items)")
        return 1 if stale else 0
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(ITEMS)} items in {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
