"""CI wrappers for the golden set (EVAL-002) and the FakeLLM smoke of the LLM harness (EVAL-003/4).

No model needed: the smoke scripts the FakeLLM from the golden expectation, so on every item that
is not a ``known_gap`` the real orchestrator must reproduce the golden tools, args and numbers
and ground every number. Known-gap items must still differ: when a gap is fixed this test fails
so the golden set gets regenerated and the gap removed from evals/golden/spec.py.
"""

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

EVALS = Path(__file__).resolve().parents[4] / "evals"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def smoke() -> dict[str, Any]:
    harness = _load("eval_llm_run", EVALS / "llm" / "run.py")
    args = argparse.Namespace(
        live=False, model="fake", few_shot=False, sample=None, seed=7, ids=None, verbose=False
    )
    summary: dict[str, Any] = harness.run(args)
    return summary


def test_golden_set_is_up_to_date_and_consistent() -> None:
    build = _load("eval_golden_build", EVALS / "golden" / "build.py")
    files, problems = build.build()
    assert problems == []
    for name, text in files.items():
        committed = (EVALS / "golden" / f"{name}.jsonl").read_text(encoding="utf-8")
        assert committed == text, f"{name}.jsonl is stale: run evals/golden/build.py"
    manifest = (EVALS / "golden" / "MANIFEST.json").read_text(encoding="utf-8")
    assert manifest == build.manifest(files)
    assert sum(t.count("\n") for t in files.values()) >= 60


def test_fake_smoke_reproduces_every_non_gap_item(smoke: dict[str, Any]) -> None:
    clean = smoke["excluding_known_gaps"]
    for key in (
        "outcome_accuracy",
        "tool_selection_accuracy",
        "args_exact_match",
        "args_ast_match",
        "numbers_correct",
        "structured_valid_first_try",
        "correct_refusal_rate",
        "injection_resisted",
    ):
        assert clean[key] == 1.0, key
    assert clean["false_refusal_rate"] == 0.0
    assert clean["failed_turns"] == 0
    grounding = clean["grounding"]
    assert grounding["unsupported_claim_rate"] == 0.0
    assert grounding["groundedness"] == 1.0
    assert grounding["claims_without_evidence"] == 0
    assert grounding["dangling_evidence_refs"] == 0


def test_known_gaps_still_differ_from_golden(smoke: dict[str, Any]) -> None:
    gaps = [r for r in smoke["rows"] if r["known_gap"]]
    assert gaps
    assert [r["id"] for r in gaps if r["outcome_ok"]] == []
