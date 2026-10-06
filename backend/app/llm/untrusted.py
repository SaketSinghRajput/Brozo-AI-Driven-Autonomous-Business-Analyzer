"""Untrusted-content wrapper and injection heuristics (TASK-SEC-010, design.md sections 76-78).

Everything that did not come from our own code or prompt files (document text, dataset cells,
tool outputs, memory, retrieved chunks) is *data*. It is wrapped in an explicit, labelled
delimiter before it reaches the model, and any delimiter look-alikes inside it are neutralised
so the content cannot close the wrapper and pose as instructions. The system section of every
prompt that receives context (`app/llm/prompts/`) tells the model that wrapped content is data.

`InjectionHeuristics` only *flags*: flags lower a part's reliability and are logged. They never
grant, block or change permissions — tool permissions are fixed in code before the LLM runs.
"""

import re
from typing import Final

TAG: Final = "untrusted_data"
_TAG_LOOKALIKE = re.compile(rf"<\s*/?\s*{TAG}", re.IGNORECASE)
_ATTR_UNSAFE = re.compile(r"[^A-Za-z0-9_.,:-]")

# ponytail: regex heuristics catch known phrasings only; a classifier model is the upgrade
# path (RES-012) once there is an eval corpus to measure it against.
_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "ignore_instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|"
        r"system|your)\b.{0,20}\b(instructions?|prompts?|rules?|directions?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    "role_tag": re.compile(
        r"<\|?\s*(im_start|im_end|system|assistant|user|tool)\s*\|?>|^\s*\[?(system|assistant)"
        r"\]?\s*:|\[/?INST\]|<<\s*/?SYS\s*>>",
        re.IGNORECASE | re.MULTILINE,
    ),
    "persona_override": re.compile(
        r"\b(you are now|from now on,? you|act as (an?|the)|pretend (to be|you are)|"
        r"new instructions?:)",
        re.IGNORECASE,
    ),
    "prompt_exfiltration": re.compile(
        r"\b(reveal|print|show|repeat|output|leak)\b.{0,30}\b(system prompt|your "
        r"(instructions|prompt|rules)|hidden prompt)",
        re.IGNORECASE | re.DOTALL,
    ),
    "tool_coercion": re.compile(
        r"\b(call|invoke|execute|run|use)\b.{0,20}\btool\b|\b(drop|delete|truncate)\s+table\b",
        re.IGNORECASE,
    ),
    "delimiter_escape": _TAG_LOOKALIKE,
}


class InjectionHeuristics:
    """Flags instruction-like patterns in untrusted text. Returns sorted flag names."""

    def scan(self, text: str) -> list[str]:
        return sorted(name for name, pattern in _PATTERNS.items() if pattern.search(text))


def _attr(value: str) -> str:
    return _ATTR_UNSAFE.sub("_", value)


def wrap_untrusted(
    content: str, *, kind: str, sources: list[str], flags: list[str] | None = None
) -> str:
    """Wrap untrusted content in a labelled data block; neutralise delimiter look-alikes."""
    safe = _TAG_LOOKALIKE.sub(lambda m: m.group(0).replace("<", "&lt;"), content)
    attrs = f'kind="{_attr(kind)}" sources="{_attr(",".join(sources))}"'
    if flags:
        attrs += f' flags="{_attr(",".join(flags))}"'
    return f"<{TAG} {attrs}>\n{safe}\n</{TAG}>"
