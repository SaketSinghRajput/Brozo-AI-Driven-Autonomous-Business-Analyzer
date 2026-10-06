"""LLM eval harness (TASK-EVAL-003) + grounding eval (TASK-EVAL-004) over the golden set.

    cd backend && uv run python ../evals/llm/run.py                        # FakeLLM smoke (CI)
    cd backend && uv run python ../evals/llm/run.py --live                 # qwen2.5:7b on Ollama
    cd backend && uv run python ../evals/llm/run.py --live --model mistral:7b --few-shot \
        --sample 24                                                        # stratified sample

Every item runs one real ``Orchestrator.handle_turn`` (evals/pipeline.py ``rig``: real planner,
tools, evidence recorder and v1 validator; in-memory persistence). Only the LLM differs:
- smoke (default): ``FakeLLMProvider`` scripted from the golden expectation (intent -> no
  drill-down -> an answer citing every fact). It checks the harness and the deterministic path;
  its scores are 1.0 by construction except where the current build differs from the golden
  expectation (``known_gap`` items), which is exactly what it should show.
- ``--live``: the local Ollama model through the production ``OllamaAdapter`` + ``LLMGateway``.

Scoring is deterministic label/evidence matching (ADR-0025): no LLM-as-judge. Writes
evals/reports/llm_<label>.json and prints a Markdown summary.
"""

import argparse
import asyncio
import json
import random
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "backend"))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from grounding import Grounding, close, summarize, turn_grounding  # noqa: E402
from pipeline import ROOT, Rig, Understood, rig, understand_fixture  # noqa: E402

from app.core.config import LLMConfig  # noqa: E402
from app.llm.fake import FakeLLMProvider  # noqa: E402
from app.llm.gateway import LLMGateway  # noqa: E402
from app.llm.ollama import OllamaAdapter  # noqa: E402
from app.llm.ports import ChatRequest, ChatResponse, LLMCapabilities, LLMProvider  # noqa: E402
from app.llm.prompts import PROMPTS_DIR, PromptRegistry  # noqa: E402
from app.llm.resilience import HostAllowList  # noqa: E402
from app.llm.router import DEFAULT_PROFILE, ModelProfile, ModelRouter  # noqa: E402
from app.orchestrator.domain import (  # noqa: E402
    AnyTurnEvent,
    ErrorEvent,
    FinalEvent,
    ToolCallEvent,
    TurnState,
)
from app.orchestrator.intent import IntentOutput  # noqa: E402
from tests.unit.orchestrator.fakes import DONE, Step, compose, num, reply, text  # noqa: E402

GOLDEN = ROOT / "evals" / "golden"
REPORTS = ROOT / "evals" / "reports"
FEW_SHOT = HERE / "fewshot"
TURN_TIMEOUT_S = 600
OUTCOME = {
    TurnState.COMPLETED: "answered",
    TurnState.REQUIRES_REVIEW: "answered",  # answered but held for review, never shown as fact
    TurnState.REFUSED: "refused",
    TurnState.NEEDS_CLARIFICATION: "clarify",
    TurnState.RECOVERED: "failed",
}
Row = dict[str, Any]


# --- inputs ---------------------------------------------------------------------------------
def load_golden() -> list[Row]:
    items = []
    for path in sorted(GOLDEN.glob("*.jsonl")):
        items += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return items


def sample(items: list[Row], n: int | None, seed: int) -> list[Row]:
    """Stratified by stratum: round-robin over shuffled strata until n items (deterministic)."""
    if n is None or n >= len(items):
        return items
    rng = random.Random(seed)  # noqa: S311 - seeded sampling, not crypto
    strata: dict[str, list[Row]] = defaultdict(list)
    for item in items:
        strata[item["stratum"]].append(item)
    queues = []
    for name in sorted(strata):
        group = strata[name][:]
        rng.shuffle(group)
        queues.append(group)
    picked: list[Row] = []
    while len(picked) < n:
        for q in queues:
            if q and len(picked) < n:
                picked.append(q.pop())
    return sorted(picked, key=lambda i: i["id"])


def fake_script(item: Row) -> list[Step]:
    """Intent as the golden says -> no drill-down -> an answer that cites every fact."""
    exp = item["expected"]
    steps: list[Step] = [reply(json.dumps(exp["intent"]))]
    if exp["current_outcome"] == "answered":
        segments = [text("Result: ")]
        for k in range(1, len(exp["numbers"]) + 1):
            segments += [num(f"n{k}"), text(". ")]
        steps += [DONE, compose(*segments)]
    return steps


# --- LLM plumbing ---------------------------------------------------------------------------
class RecordingProvider:
    """Wraps a provider; records prompt id, latency, tokens and the raw response per call."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.capabilities: LLMCapabilities = inner.capabilities
        self.calls: list[Row] = []

    async def chat(self, req: ChatRequest) -> ChatResponse:
        started = time.perf_counter()
        call: Row = {"prompt_id": req.prompt_id}
        try:
            resp = await self.inner.chat(req)
        except BaseException as exc:
            call |= {"latency_s": time.perf_counter() - started, "error": type(exc).__name__}
            self.calls.append(call)
            raise
        call |= {
            "latency_s": time.perf_counter() - started,
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "text": resp.text,
            "tool_calls": [c.name for c in resp.tool_calls],
        }
        self.calls.append(call)
        return resp

    def stream(self, req: ChatRequest) -> Any:
        return self.inner.stream(req)


async def _no_sleep(_: float) -> None:
    return None


def build_gateway(
    provider: LLMProvider, prompts: PromptRegistry, config: LLMConfig, *, live: bool
) -> tuple[LLMGateway, RecordingProvider]:
    """The gateway_from_config composition, with the provider wrapped for recording."""
    recording = RecordingProvider(provider)
    profile = ModelProfile(
        name=DEFAULT_PROFILE,
        provider="ollama" if live else "fake",
        model_version=f"ollama:{config.model}" if live else "fake-1",
        max_context=config.num_ctx if live else 32_768,
        capabilities=provider.capabilities,
    )
    gw = LLMGateway(
        ModelRouter({DEFAULT_PROFILE: profile}),
        {DEFAULT_PROFILE: recording},
        prompts,
        max_attempts=config.max_attempts,
        timeout_s=config.timeout_s,
        sleep=asyncio.sleep if live else _no_sleep,
    )
    return gw, recording


def few_shot_prompts(tmp: Path) -> PromptRegistry:
    """The production prompt set with intent/resolve replaced by evals/llm/fewshot/."""
    root = tmp / "prompts"
    shutil.copytree(PROMPTS_DIR, root)
    for src in (FEW_SHOT / "intent").iterdir():
        shutil.copy(src, root / "intent" / src.name)
    return PromptRegistry(root)


# --- scoring --------------------------------------------------------------------------------
def _canon(value: Any, *, unordered: bool) -> Any:
    if isinstance(value, dict):
        return {k: _canon(v, unordered=unordered) for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [_canon(v, unordered=unordered) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True)) if unordered else items
    return value


def args_match(observed: list[Any], expected: list[Any], *, unordered: bool) -> bool:
    """Exact: same JSON. AST: same argument tree ignoring list order (filters, dimensions).

    The executor stores validated args with the input model's defaults filled in, so observed
    args are projected onto the keys the plan step set before comparing."""
    if len(observed) != len(expected):
        return False
    projected = [_project(o, e) for o, e in zip(observed, expected, strict=True)]
    return _canon(projected, unordered=unordered) == _canon(expected, unordered=unordered)


def _project(observed: Any, expected: Any) -> Any:
    if isinstance(observed, dict) and isinstance(expected, dict):
        return {k: _project(observed.get(k), v) for k, v in expected.items()}
    return observed


def llm_intent(calls: list[Row]) -> Row | None:
    """The last intent the model produced that parses as IntentOutput (after repair, if any)."""
    for call in reversed(calls):
        if call["prompt_id"].startswith("intent.resolve") and call.get("text"):
            try:
                out = IntentOutput.model_validate_json(call["text"], strict=True)
            except ValueError:
                continue
            return out.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    return None


def score(item: Row, r: Rig, events: list[AnyTurnEvent], calls: list[Row]) -> Row:
    exp = item["expected"]
    final = events[-1]
    assert isinstance(final, FinalEvent)  # noqa: S101
    record = r.recorder.records[0]
    trace = record.trace
    observed = OUTCOME[final.state]
    code = trace.get("refusal")
    outcome_ok = observed == exp["outcome"] and (
        observed != "refused" or code in exp["refusal_codes"]
    )
    trace_calls = trace["tool_calls"]
    assert isinstance(trace_calls, list)  # noqa: S101
    step_ids = [str(tc["step_id"]) for tc in trace_calls if isinstance(tc, dict)]
    plan = [run for run, sid in zip(r.executor.runs, step_ids, strict=False) if sid[0] == "S"]
    tools = [name for name, _, _ in plan]
    args = [args for _, args, _ in plan]
    values = [
        float(res.output["value"])
        for _, _, res in plan
        if res.status == "ok"
        and res.output is not None
        and isinstance(res.output.get("value"), int | float)
    ]
    expected_values = [float(n["value"]) for n in exp["numbers"]]
    intent = llm_intent(calls)
    structured = [c for c in calls if not c["prompt_id"].startswith("tool_loop")]
    repairs = sum("+" in c["prompt_id"] for c in structured)
    errors = [e.error for e in events if isinstance(e, ErrorEvent)]
    grounding = turn_grounding(record, r.validation.results, r.executor.evidence, expected_values)
    rejected = sum(isinstance(e, ToolCallEvent) and e.status == "rejected" for e in events)
    ranking = [
        [i["key"] for i in res.output["ranking"]["items"][:3]]
        for name, _, res in plan
        if name == "analytics.rank" and res.output is not None
    ]
    ranking_top = ranking[0] if ranking else []
    # Correct numbers: the turn answered, every plan value equals the golden value in order, and
    # a ranking (which has no scalar) lists the same top members.
    numbers_ok = (
        observed == "answered"
        and len(values) == len(expected_values)
        and all(close(a, b) for a, b in zip(values, expected_values, strict=True))
        and ranking_top == exp["ranking_top"]
    )
    tool_ok = tools == exp["tools"]
    # Injection resisted: nothing the injected text asked for got through (no number outside
    # evidence, no dangling evidence, no call to a tool that was not offered). Whether the turn
    # answered correctly is scored by outcome/numbers, not here.
    injection_ok = grounding.unbound == 0 and grounding.dangling == 0 and rejected == 0
    return {
        "id": item["id"],
        "dataset": item["dataset"],
        "stratum": item["stratum"],
        "injection": item["injection"],
        "known_gap": bool(item["known_gap"]),
        "expected_outcome": exp["outcome"],
        "observed_outcome": observed,
        "state": final.state.value,
        "validation_status": grounding.status,
        "refusal_code": code,
        "outcome_ok": outcome_ok,
        "route_ok": intent is not None and intent.get("route") == exp["route"],
        "objective_ok": intent is not None and intent.get("objective") == exp["objective"],
        "intent": intent,
        "tools_expected": exp["tools"],
        "tools_observed": tools,
        "tool_ok": tool_ok,
        "args_exact": tool_ok and args_match(args, exp["tool_args"], unordered=False),
        "args_ast": tool_ok and args_match(args, exp["tool_args"], unordered=True),
        "numbers_ok": numbers_ok,
        "drilldown_calls": sum(sid[0] == "L" for sid in step_ids),
        "rejected_tool_calls": rejected,
        "llm_calls": len(calls),
        "structured_first": len(structured) - repairs,
        "structured_repairs": repairs,
        "structured_final_invalid": sum("invalid after one repair" in e.message for e in errors),
        "errors": [f"{e.error_code}: {e.message}" for e in errors],
        "ranking_top": ranking_top,
        "prompt_tokens": sum(c.get("prompt_tokens", 0) for c in calls),
        "completion_tokens": sum(c.get("completion_tokens", 0) for c in calls),
        "llm_latency_s": [round(c["latency_s"], 3) for c in calls],
        "grounding": {**grounding.__dict__, "grounded": grounding.grounded},
        "injection_ok": injection_ok if item["injection"] else None,
    }


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 3)


def _rate(rows: list[Row], key: str) -> float | None:
    return round(sum(bool(r[key]) for r in rows) / len(rows), 4) if rows else None


def _ratio(num_: float, den: float) -> float | None:
    return round(num_ / den, 4) if den else None


def aggregate(rows: list[Row], items: dict[str, Row]) -> Row:
    answerable = [r for r in rows if r["expected_outcome"] == "answered"]
    refusals = [r for r in rows if r["expected_outcome"] != "answered"]
    firsts = sum(r["structured_first"] for r in rows)
    call_s = [x for r in rows for x in r["llm_latency_s"]]
    turn_s = [r["turn_s"] for r in rows]
    stored = [
        (
            items[r["id"]],
            Grounding(**{k: v for k, v in r["grounding"].items() if k != "grounded"}),
            r["outcome_ok"],
        )
        for r in rows
    ]
    return {
        "items": len(rows),
        "outcome_accuracy": _rate(rows, "outcome_ok"),
        "route_accuracy": _rate(rows, "route_ok"),
        "objective_accuracy": _rate(rows, "objective_ok"),
        "tool_selection_accuracy": _rate(rows, "tool_ok"),
        "args_exact_match": _rate(answerable, "args_exact"),
        "args_ast_match": _rate(answerable, "args_ast"),
        "numbers_correct": _rate(answerable, "numbers_ok"),
        "structured_valid_first_try": _ratio(
            firsts - sum(r["structured_repairs"] for r in rows), firsts
        ),
        "structured_valid_final": _ratio(
            firsts - sum(r["structured_final_invalid"] for r in rows), firsts
        ),
        "correct_refusal_rate": _rate(refusals, "outcome_ok"),
        "false_refusal_rate": _ratio(
            sum(r["observed_outcome"] in ("refused", "clarify") for r in answerable),
            len(answerable),
        ),
        "failed_turns": sum(r["observed_outcome"] == "failed" for r in rows),
        "injection_resisted": _rate([r for r in rows if r["injection"]], "injection_ok"),
        "drilldown_calls": sum(r["drilldown_calls"] for r in rows),
        "rejected_tool_calls": sum(r["rejected_tool_calls"] for r in rows),
        "turn_latency_s": {"p50": _pct(turn_s, 0.5), "p95": _pct(turn_s, 0.95)},
        "llm_call_latency_s": {"p50": _pct(call_s, 0.5), "p95": _pct(call_s, 0.95)},
        "tokens": {
            "prompt": sum(r["prompt_tokens"] for r in rows),
            "completion": sum(r["completion_tokens"] for r in rows),
            "per_turn_mean": round(
                statistics.fmean(r["prompt_tokens"] + r["completion_tokens"] for r in rows), 1
            )
            if rows
            else None,
        },
        "grounding": summarize(stored),
    }


# --- run ------------------------------------------------------------------------------------
async def run_item(
    item: Row, data: Understood, prompts: PromptRegistry, config: LLMConfig, live: bool
) -> Row:
    provider: LLMProvider
    if live:
        provider = OllamaAdapter(
            endpoint=str(config.endpoint),
            model=config.model,
            allow_list=HostAllowList(config.allowed_hosts),
            timeout_s=config.timeout_s,
            num_ctx=config.num_ctx,
        )
    else:
        provider = FakeLLMProvider(fake_script(item))
    gw, recording = build_gateway(provider, prompts, config, live=live)
    r = rig(data, item["question"], llm=gw, prompts=prompts)
    started = time.perf_counter()
    events = [e async for e in r.orchestrator.handle_turn(r.request)]
    turn_s = round(time.perf_counter() - started, 3)
    return score(item, r, events, recording.calls) | {"turn_s": turn_s}


def label_for(args: argparse.Namespace) -> str:
    if not args.live:
        return "fake"
    name = re.sub(r"[^a-z0-9]+", "-", args.model.lower()).strip("-")
    return f"{name}{'-fewshot' if args.few_shot else ''}"


METRICS = [
    "items",
    "outcome_accuracy",
    "route_accuracy",
    "objective_accuracy",
    "tool_selection_accuracy",
    "args_exact_match",
    "args_ast_match",
    "numbers_correct",
    "structured_valid_first_try",
    "structured_valid_final",
    "correct_refusal_rate",
    "false_refusal_rate",
    "failed_turns",
    "injection_resisted",
    "turn_latency_s",
    "llm_call_latency_s",
    "tokens",
]
GROUNDING = [
    "unsupported_claim_rate",
    "groundedness",
    "answerable_grounded_rate",
    "correct_refusal_rate",
    "validation_status",
]


def markdown(summary: Row, title: str) -> str:
    lines = [f"### {title}", "", "| metric | all | excl. known gaps |", "|---|---|---|"]
    both = (summary["all"], summary["excluding_known_gaps"])
    lines += [f"| {k} | {both[0][k]} | {both[1][k]} |" for k in METRICS]
    lines += [
        f"| grounding.{k} | {both[0]['grounding'][k]} | {both[1]['grounding'][k]} |"
        for k in GROUNDING
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> Row:
    config = LLMConfig(model=args.model)
    items = load_golden()
    if args.ids:
        items = [i for i in items if i["id"] in set(args.ids)]
    items = sample(items, args.sample, args.seed)
    by_id = {i["id"]: i for i in items}
    rows: list[Row] = []
    with tempfile.TemporaryDirectory() as tmp:
        prompts = few_shot_prompts(Path(tmp)) if args.few_shot else PromptRegistry()
        cache: dict[str, Understood] = {}
        started = time.perf_counter()
        for n, item in enumerate(items, start=1):
            name = item["dataset"]
            if name not in cache:
                cache[name] = understand_fixture(name, Path(tmp) / "data")
            coro = run_item(item, cache[name], prompts, config, args.live)
            row = asyncio.run(asyncio.wait_for(coro, TURN_TIMEOUT_S))
            rows.append(row)
            if args.verbose:
                print(
                    f"[{n}/{len(items)}] {row['id']} {row['observed_outcome']}"
                    f" ok={row['outcome_ok']} tools={row['tool_ok']} {row['turn_s']}s",
                    file=sys.stderr,
                    flush=True,
                )
        wall_s = round(time.perf_counter() - started, 1)
    return {
        "label": label_for(args),
        "mode": "live" if args.live else "fake",
        "model": args.model if args.live else "FakeLLMProvider",
        "few_shot": args.few_shot,
        "sample": args.sample,
        "seed": args.seed,
        "wall_s": wall_s,
        "golden_manifest_sha256": {
            k: v["sha256"]
            for k, v in json.loads((GOLDEN / "MANIFEST.json").read_text("utf-8"))["files"].items()
        },
        "all": aggregate(rows, by_id),
        "excluding_known_gaps": aggregate([r for r in rows if not r["known_gap"]], by_id),
        "rows": rows,
    }


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LLM + grounding eval over the golden set.")
    parser.add_argument("--live", action="store_true", help="use the local Ollama model")
    parser.add_argument("--model", default=LLMConfig().model)
    parser.add_argument("--few-shot", action="store_true", help="few-shot intent prompt")
    parser.add_argument("--sample", type=int, default=None, help="stratified sample size")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--ids", nargs="*", default=None, help="only these item ids")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--no-write", action="store_true", help="do not write the report")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = parse()
    summary = run(args)
    if not args.no_write:
        out = args.out or REPORTS / f"llm_{summary['label']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        out.write_text(body, encoding="utf-8", newline="\n")
    items = summary["all"]["items"]
    print(markdown(summary, f"{summary['label']} ({items} items, {summary['wall_s']} s)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
