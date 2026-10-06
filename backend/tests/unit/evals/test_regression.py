"""TASK-TEST-008: default plan, capability report and metric totals per fixture are unchanged.

On an intended change run ``uv run python ../evals/regression/run.py --write`` and review the
diff of evals/golden/regression/*.json in the PR.
"""

import importlib.util
import sys
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[4] / "evals" / "regression" / "run.py"


def test_regression_snapshots_unchanged() -> None:
    spec = importlib.util.spec_from_file_location("eval_regression_run", RUNNER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_regression_run"] = module
    spec.loader.exec_module(module)
    rendered = module.render()
    assert set(rendered) == {"retail", "saas", "agriculture", "insufficient", "ambiguous"}
    problems = module.check(rendered)
    assert problems == [], "\n\n".join(problems)
