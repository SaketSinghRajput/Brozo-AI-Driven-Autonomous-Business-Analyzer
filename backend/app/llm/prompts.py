"""Prompt registry (TASK-LLM-005, design.md section 31).

Prompt text lives only in `app/llm/prompts/<task>/<name>.md` (+ `<name>.meta.json`); a unit
test forbids inline prompt literals elsewhere in `app/`. The id is `<task>.<name>`; the version
is a content hash of the prompt file plus its meta, so any edit yields a new version and every
LLM call log carries exactly which text was sent.

File format: an optional `### system` section followed by a `### user` section. Variables use
`$name` (string.Template), so JSON braces in prompt text need no escaping. Meta (JSON; chosen
over YAML to avoid a parser dependency): `{"variables": [...], "output_schema": "Name" | null}`.
"""

import hashlib
import json
import re
from pathlib import Path
from string import Template

from pydantic import BaseModel, ConfigDict

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_SECTION = re.compile(r"^### (system|user)\s*$", re.MULTILINE)


class PromptTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: str
    system: str
    user: str
    variables: tuple[str, ...]
    output_schema: str | None = None

    def render(self, **values: str) -> tuple[str, str]:
        """Return (system, user) with variables substituted; exact variable set required."""
        if set(values) != set(self.variables):
            raise ValueError(
                f"prompt {self.id} needs variables {sorted(self.variables)}, got {sorted(values)}"
            )
        return (
            Template(self.system).substitute(values),
            Template(self.user).substitute(values),
        )


def _parse(prompt_id: str, md: str) -> tuple[str, str]:
    parts = _SECTION.split(md)
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if "user" not in sections or parts[0].strip():
        raise ValueError(f"prompt {prompt_id}: needs '### user' (and optional '### system') only")
    return sections.get("system", ""), sections["user"]


class PromptRegistry:
    def __init__(self, root: Path = PROMPTS_DIR) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        for md_path in sorted(root.glob("*/*.md")):
            meta_path = md_path.with_suffix(".meta.json")
            prompt_id = f"{md_path.parent.name}.{md_path.stem}"
            md_bytes, meta_bytes = md_path.read_bytes(), meta_path.read_bytes()
            meta = json.loads(meta_bytes)
            system, user = _parse(prompt_id, md_bytes.decode("utf-8"))
            self._templates[prompt_id] = PromptTemplate(
                id=prompt_id,
                version=hashlib.sha256(md_bytes + b"\0" + meta_bytes).hexdigest()[:12],
                system=system,
                user=user,
                variables=tuple(meta["variables"]),
                output_schema=meta.get("output_schema"),
            )

    def get(self, prompt_id: str) -> PromptTemplate:
        try:
            return self._templates[prompt_id]
        except KeyError:
            raise KeyError(f"unknown prompt {prompt_id!r}") from None

    def ids(self) -> list[str]:
        return sorted(self._templates)
