"""TASK-LLM-005: prompt registry + the lint rule forbidding inline prompt text in app code."""

import ast
from pathlib import Path

import pytest

from app.llm.prompts import PromptRegistry

APP_DIR = Path(__file__).resolve().parents[3] / "app"

# Calls that carry text to a model. A string literal argument that looks like prompt text
# (multi-line, or long prose) is forbidden outside app/llm/prompts/ (design.md section 31).
MODEL_BOUND_CALLS = {
    "Message",
    "ChatRequest",
    "PromptTemplate",
    "structured",
    "tool_step",
    "stream_text",
    "chat",
    "stream",
}
MAX_LITERAL = 60


def _prompt_like(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "\n" in node.value or len(node.value) > MAX_LITERAL
    if isinstance(node, ast.JoinedStr):
        text = "".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        return "\n" in text or len(text) > MAX_LITERAL
    return False


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def inline_prompt_violations(source: str, filename: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Call) and _callee(node) in MODEL_BOUND_CALLS:
            args = [*node.args, *(k.value for k in node.keywords)]
            if any(_prompt_like(a) for a in args):
                found.append(f"{filename}:{node.lineno} {_callee(node)}(...)")
    return found


def test_no_inline_prompts_in_app_code() -> None:
    violations = []
    for path in APP_DIR.rglob("*.py"):
        if "prompts" in path.relative_to(APP_DIR).parts:
            continue
        violations += inline_prompt_violations(path.read_text("utf-8"), str(path))
    assert violations == [], "prompt text belongs in app/llm/prompts/: " + "; ".join(violations)


def test_lint_rule_catches_inline_prompts() -> None:
    bad = (
        'Message(role="system", content="You are a helpful analyst.\\nAnswer briefly.")\n'
        'gw.stream_text(task, content=f"Summarise the following quarterly results for {x} '
        'in three short bullets")\n'
    )
    assert len(inline_prompt_violations(bad, "bad.py")) == 2
    assert inline_prompt_violations('Message(role="user", content=text)', "ok.py") == []


def test_registry_loads_shipped_prompts_with_content_hash_versions() -> None:
    registry = PromptRegistry()
    assert {"intent.classify", "common.structured_repair"} <= set(registry.ids())
    template = registry.get("intent.classify")
    assert len(template.version) == 12
    system, user = template.render(question="Revenue by region?")
    assert "untrusted_data" in system
    assert user == "Question: Revenue by region?"


def test_intent_prompt_examples_match_the_intent_model() -> None:
    """B6 + F2: the default intent prompt carries few-shot examples, every example answer is a
    valid IntentOutput, and it asks for no field the model forbids (e.g. "clarification")."""
    from app.orchestrator.intent import IntentOutput

    system, _ = PromptRegistry().get("intent.resolve").render(question="q", today="2026-09-28")
    answers = [line.removeprefix("A: ") for line in system.splitlines() if line.startswith("A: ")]
    assert len(answers) >= 8
    for answer in answers:
        IntentOutput.model_validate_json(answer)  # extra="forbid": unknown fields fail
    assert '"clarification"' not in system
    assert any('"compare_to": "previous_year"' in a for a in answers)
    assert "time_dimension" in system


def _write(root: Path, body: str) -> None:
    (root / "demo").mkdir(exist_ok=True)
    (root / "demo" / "p.md").write_text(body, "utf-8")
    (root / "demo" / "p.meta.json").write_text('{"variables": ["x"], "output_schema": null}')


def test_version_changes_when_text_changes(tmp_path: Path) -> None:
    _write(tmp_path, "### user\nHello $x\n")
    first = PromptRegistry(tmp_path).get("demo.p").version
    _write(tmp_path, "### user\nHello there $x\n")
    assert PromptRegistry(tmp_path).get("demo.p").version != first


def test_render_requires_exact_variables(tmp_path: Path) -> None:
    _write(tmp_path, "### user\nHello $x\n")
    template = PromptRegistry(tmp_path).get("demo.p")
    with pytest.raises(ValueError, match="needs variables"):
        template.render()
    with pytest.raises(ValueError, match="needs variables"):
        template.render(x="a", y="b")


def test_malformed_prompt_file_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "stray text\n### user\nHi $x\n")
    with pytest.raises(ValueError, match="### user"):
        PromptRegistry(tmp_path)


def test_unknown_prompt_id() -> None:
    with pytest.raises(KeyError, match="unknown prompt"):
        PromptRegistry().get("nope.nope")
