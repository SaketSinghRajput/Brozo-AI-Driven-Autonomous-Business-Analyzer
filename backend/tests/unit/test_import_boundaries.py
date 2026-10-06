"""The import-boundary check (TASK-FOUND-010) passes on the real code and fails on a violation."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
LINT_IMPORTS = shutil.which("lint-imports")

pytestmark = pytest.mark.skipif(LINT_IMPORTS is None, reason="import-linter not installed")


def _run(cwd: Path, config: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(cwd)}
    return subprocess.run(  # noqa: S603  # fixed executable, test-controlled args
        [str(LINT_IMPORTS), "--config", str(config)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_real_codebase_respects_contracts() -> None:
    result = _run(BACKEND, BACKEND / "pyproject.toml")
    assert result.returncode == 0, result.stdout + result.stderr


def test_violation_is_detected(tmp_path: Path) -> None:
    pkg = tmp_path / "demo"
    (pkg / "core").mkdir(parents=True)
    (pkg / "api").mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "api" / "__init__.py").write_text("")
    (pkg / "core" / "__init__.py").write_text("import demo.api  # forbidden\n")
    config = tmp_path / "importlinter.toml"
    config.write_text(
        '[tool.importlinter]\nroot_package = "demo"\n\n'
        "[[tool.importlinter.contracts]]\n"
        'name = "core must not import api"\ntype = "forbidden"\n'
        'source_modules = ["demo.core"]\nforbidden_modules = ["demo.api"]\n'
    )
    result = _run(tmp_path, config)
    assert result.returncode != 0
    assert "broken" in result.stdout.lower()
