"""Smoke test: the data-understanding eval harness runs in CI and meets its baseline (EVAL-001)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

HARNESS = Path(__file__).resolve().parents[5] / "evals" / "data_understanding" / "run.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("du_eval_run", HARNESS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["du_eval_run"] = module
    spec.loader.exec_module(module)
    return module


def test_harness_meets_baseline_and_fixture_domains() -> None:
    harness = _load()
    report = harness.run()
    assert harness.check(report) == []
    domains = report["domains"]
    assert (domains["retail"]["status"], domains["retail"]["primary"]) == ("KNOWN", "retail")
    assert (domains["saas"]["status"], domains["saas"]["primary"]) == ("KNOWN", "saas")
    assert domains["agriculture"]["primary"] == "agriculture"
    assert domains["insufficient"]["primary"] == "retail"
    assert domains["ambiguous"]["status"] in {"UNKNOWN", "AMBIGUOUS"}
    assert "DOMAIN_UNCERTAIN" in report["semantic"]["ambiguous"]["data"]["review_flags"]
    assert report["metrics"]["domain_accuracy"] == 1.0


def test_macro_f1() -> None:
    harness = _load()
    assert harness.macro_f1([("a", "a"), ("b", "b")]) == 1.0
    assert harness.macro_f1([("a", "b"), ("b", "a")]) == 0.0
