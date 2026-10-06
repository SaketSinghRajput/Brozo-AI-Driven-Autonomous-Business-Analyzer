"""Tests for scripts/check_forbidden_providers.py (TASK-SEC-001).

Run with: uv run --project backend pytest scripts/tests   (from the repo root)
scripts/tests/conftest.py puts scripts/ on sys.path so this can import the checker directly.
"""

from pathlib import Path

import check_forbidden_providers as cfp

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_fails_on_fixture_with_openai_import(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (project / "bad.py").write_text("import openai\n", encoding="utf-8")

    violations = cfp.scan(project, allowlist=set())

    assert len(violations) == 1
    assert violations[0].provider == "OpenAI"
    assert violations[0].path.name == "bad.py"


def test_passes_on_real_repo_tree() -> None:
    allowlist = cfp.load_allowlist(cfp.DEFAULT_ALLOWLIST)

    violations = cfp.scan(cfp.REPO_ROOT, allowlist)

    assert not violations, "unexpected forbidden-provider references:\n" + "\n".join(
        v.format() for v in violations
    )


def test_allowlist_exempts_the_sec_001_fixture() -> None:
    allowlist = cfp.load_allowlist(cfp.DEFAULT_ALLOWLIST)
    rel = (FIXTURES / "hosted_openai.py").relative_to(cfp.REPO_ROOT).as_posix()
    assert rel in allowlist, "the fixture must be allow-listed or the real-tree scan would fail"

    violations = cfp.scan(cfp.REPO_ROOT, allowlist)
    assert not any(v.path.name == "hosted_openai.py" for v in violations)


def test_allowlist_works_without_it_too(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / "scripts" / "tests" / "fixtures").mkdir(parents=True)
    target = project / "scripts" / "tests" / "fixtures" / "hosted_openai.py"
    target.write_text("import openai\n", encoding="utf-8")

    assert cfp.scan(project, allowlist=set())  # violation without the allow-list entry

    allowed = {"scripts/tests/fixtures/hosted_openai.py"}
    assert cfp.scan(project, allowlist=allowed) == []  # none once allow-listed


def test_ollama_is_never_flagged(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (project / "client.py").write_text(
        "import ollama\n\nHOST = 'http://127.0.0.1:11434'  # OLLAMA_HOST\n",
        encoding="utf-8",
    )
    (project / ".env.example").write_text("OLLAMA_HOST=http://127.0.0.1:11434\n", encoding="utf-8")

    assert cfp.scan(project, allowlist=set()) == []


def test_docs_directory_is_skipped(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / "docs").mkdir(parents=True)
    # CLAUDE.md-style prose naming a forbidden provider in a .py-looking path under docs/
    (project / "docs" / "example.py").write_text("import openai\n", encoding="utf-8")

    assert cfp.scan(project, allowlist=set()) == []
