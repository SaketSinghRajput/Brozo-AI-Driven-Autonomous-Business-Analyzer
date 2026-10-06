#!/usr/bin/env python3
"""Fail if hosted-AI-provider SDKs, packages or hostnames appear in product code (TASK-SEC-001).

CLAUDE.md section 20 ("Local AI Policy") forbids the product from calling any hosted AI API at
runtime: OpenAI, Gemini/Google Generative AI/Vertex AI, Claude/Anthropic, Groq, OpenRouter,
Cohere, hosted Mistral, Together, and Replicate. Local/self-hosted engines (Ollama, llama.cpp,
vLLM, local Hugging Face models) are explicitly allowed and must never be flagged.

Scope: this scans code files, dependency manifests, and env templates for imports/package names
and hostnames of the forbidden providers, per the enforcement list in CLAUDE.md section 20.

Deliberately NOT scanned: `docs/` and any Markdown/prose file. Those legitimately name hosted
providers when discussing what is forbidden and why (this very file's docstring is an example);
flagging prose would just train everyone to ignore the check. Directories that are not part of
the shipped project (`.git`, `node_modules`, `.venv`/`venv`, `.claude` which also holds git
worktrees, build/cache dirs) are skipped outright.

A path can be exempted via the allow-list file (default: scripts/forbidden_providers_allowlist.txt),
one `<repo-relative-path> | <reason>` per line. Per TASK-SEC-001's acceptance criteria, adding an
entry requires ARCH approval, recorded in the allow-list file's own header.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "forbidden_providers_allowlist.txt"
# This file itself necessarily contains every forbidden hostname/package name as *data* (the
# PROVIDERS table below) so it can recognize them elsewhere. That's not a policy exception, so it
# doesn't belong in the allow-list; it's excluded structurally instead.
SELF_PATH = Path(__file__).resolve()

SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    ".claude",  # also holds git worktrees under .claude/worktrees
    ".claude-flow",
    "docs",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".import_linter_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
MANIFEST_NAMES = {"pyproject.toml", "uv.lock", "package.json", "package-lock.json"}


def _is_manifest(name: str) -> bool:
    return name in MANIFEST_NAMES or (name.startswith("requirements") and name.endswith(".txt"))


def _is_env_template(name: str) -> bool:
    return name.startswith(".env") and name != ".env"  # .env itself is git-ignored, never in git


@dataclass(frozen=True)
class Provider:
    name: str
    import_names: tuple[str, ...] = ()  # python module roots: `import X`, `from X import ...`
    js_specifiers: tuple[str, ...] = ()  # JS/TS: from "X", require("X"), import("X")
    package_names: tuple[str, ...] = ()  # manifest dependency names
    hostnames: tuple[str, ...] = ()


# Derived from CLAUDE.md section 20's forbidden-provider list and its CI-enforcement hostname
# examples. Local engines (Ollama, llama.cpp, vLLM, local HF models) are intentionally absent.
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        name="OpenAI",
        import_names=("openai",),
        js_specifiers=("openai",),
        package_names=("openai",),
        hostnames=("api.openai.com",),
    ),
    Provider(
        name="Anthropic / Claude API",
        import_names=("anthropic",),
        js_specifiers=("@anthropic-ai/sdk", "anthropic"),
        package_names=("anthropic", "@anthropic-ai/sdk"),
        hostnames=("api.anthropic.com",),
    ),
    Provider(
        name="Google Gemini / Generative AI / Vertex AI",
        import_names=("google.generativeai", "google.genai", "vertexai"),
        js_specifiers=("@google/generative-ai", "@google-cloud/vertexai", "@google/genai"),
        package_names=(
            "google-generativeai",
            "google-genai",
            "google-cloud-aiplatform",
            "@google/generative-ai",
            "@google-cloud/vertexai",
            "@google/genai",
        ),
        hostnames=("generativelanguage.googleapis.com", "aiplatform.googleapis.com"),
    ),
    Provider(
        name="Groq",
        import_names=("groq",),
        js_specifiers=("groq-sdk",),
        package_names=("groq", "groq-sdk"),
        hostnames=("api.groq.com",),
    ),
    Provider(
        name="OpenRouter",
        package_names=("openrouter",),
        hostnames=("openrouter.ai",),
    ),
    Provider(
        name="Cohere",
        import_names=("cohere",),
        js_specifiers=("cohere-ai",),
        package_names=("cohere", "cohere-ai"),
        hostnames=("api.cohere.ai", "api.cohere.com"),
    ),
    Provider(
        name="Hosted Mistral API",
        import_names=("mistralai",),
        js_specifiers=("@mistralai/mistralai",),
        package_names=("mistralai", "@mistralai/mistralai"),
        hostnames=("api.mistral.ai",),
    ),
    Provider(
        name="Together",
        import_names=("together",),
        package_names=("together",),
        hostnames=("api.together.xyz", "api.together.ai"),
    ),
    Provider(
        name="Replicate",
        import_names=("replicate",),
        js_specifiers=("replicate",),
        package_names=("replicate",),
        hostnames=("api.replicate.com",),
    ),
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line_no: int
    provider: str
    matched: str

    def format(self) -> str:
        rel = self.path.relative_to(REPO_ROOT) if self.path.is_absolute() else self.path
        return f"{rel}:{self.line_no}: forbidden provider [{self.provider}]: {self.matched!r}"


def _ident_pattern(name: str) -> re.Pattern[str]:
    # Bounded so "openai" doesn't match inside an unrelated longer identifier, but does match
    # dotted/hyphenated forms like "google.generativeai" or "@anthropic-ai/sdk".
    return re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])")


def _python_import_patterns(name: str) -> list[re.Pattern[str]]:
    root = re.escape(name.split(".")[0])
    return [
        re.compile(rf"^\s*import\s+{re.escape(name)}(?:[.\s]|$)"),
        re.compile(rf"^\s*from\s+{re.escape(name)}(?:\.\w+)*\s+import\b"),
        re.compile(rf"^\s*import\s+{root}\."),
    ]


def _js_import_patterns(specifier: str) -> list[re.Pattern[str]]:
    spec = re.escape(specifier)
    return [
        re.compile(rf"""from\s+['"]{spec}(?:/[\w.-]*)?['"]"""),
        re.compile(rf"""require\(\s*['"]{spec}(?:/[\w.-]*)?['"]\s*\)"""),
        re.compile(rf"""import\(\s*['"]{spec}(?:/[\w.-]*)?['"]\s*\)"""),
    ]


def _scan_lines(path: Path, lines: list[str]) -> list[Violation]:
    violations: list[Violation] = []
    suffix = path.suffix
    name = path.name
    is_code = suffix in CODE_SUFFIXES
    is_manifest = _is_manifest(name)
    is_env = _is_env_template(name)

    for lineno, line in enumerate(lines, start=1):
        for provider in PROVIDERS:
            if is_code and suffix == ".py":
                for imp in provider.import_names:
                    for pattern in _python_import_patterns(imp):
                        if pattern.search(line):
                            violations.append(Violation(path, lineno, provider.name, line.strip()))
            if is_code and suffix != ".py":
                for spec in provider.js_specifiers:
                    for pattern in _js_import_patterns(spec):
                        if pattern.search(line):
                            violations.append(Violation(path, lineno, provider.name, line.strip()))
            if is_manifest:
                for pkg in provider.package_names:
                    if _ident_pattern(pkg).search(line):
                        violations.append(Violation(path, lineno, provider.name, line.strip()))
            if is_code or is_manifest or is_env:
                for host in provider.hostnames:
                    if host in line:
                        violations.append(Violation(path, lineno, provider.name, line.strip()))
    return violations


def _iter_scannable_files(root: Path) -> list[Path]:
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIR_NAMES:
                    stack.append(entry)
                continue
            if entry.resolve() == SELF_PATH:
                continue
            if entry.suffix in CODE_SUFFIXES or _is_manifest(entry.name) or _is_env_template(
                entry.name
            ):
                files.append(entry)
    return files


def load_allowlist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    allowed: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        rel_path = line.split("|", 1)[0].strip()
        if rel_path:
            allowed.add(rel_path.replace("\\", "/"))
    return allowed


def scan(root: Path, allowlist: set[str]) -> list[Violation]:
    violations: list[Violation] = []
    for path in _iter_scannable_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in allowlist:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        violations.extend(_scan_lines(path, text.splitlines()))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Repo root to scan")
    parser.add_argument(
        "--allowlist", type=Path, default=DEFAULT_ALLOWLIST, help="Allow-list file"
    )
    args = parser.parse_args(argv)

    allowlist = load_allowlist(args.allowlist)
    violations = scan(args.root, allowlist)

    if violations:
        print(f"Forbidden hosted-AI-provider references found ({len(violations)}):", file=sys.stderr)
        for v in violations:
            print(f"  {v.format()}", file=sys.stderr)
        print(
            "\nCLAUDE.md section 20 forbids hosted AI providers at runtime. "
            "If this is a genuine false positive, add it to "
            f"{args.allowlist} with ARCH approval and a reason.",
            file=sys.stderr,
        )
        return 1

    print("No forbidden hosted-AI-provider references found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
