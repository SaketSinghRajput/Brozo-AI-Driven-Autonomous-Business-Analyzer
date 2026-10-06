"""ContextBuilder (TASK-LLM-006, design.md sections 31, 38, 77).

Assembles one prompt: the template's system section (trusted, fixed) → ranked context parts →
the rendered user section. Guarantees:
- the token budget is never exceeded (parts that do not fit are dropped, never truncated);
- per-kind caps stop one source (e.g. documents) from crowding out the rest;
- untrusted parts are always wrapped + labelled and never placed in the system role;
- untrusted parts are scanned by InjectionHeuristics; a flag halves the part's reliability
  (ranking weight) and is logged — it never grants or blocks anything.
"""

import logging
import math
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.llm.errors import LLMFailure, LLMFailureReason
from app.llm.ports import Message
from app.llm.prompts import PromptTemplate
from app.llm.untrusted import InjectionHeuristics, wrap_untrusted

log = logging.getLogger("app.llm.context")

PartKind = Literal[
    "system_policy",
    "question",
    "dataset",
    "semantic",
    "tool_results",
    "evidence",
    "memory",
    "documents",
    "conversation",
]

DEFAULT_KIND_CAPS: dict[str, int] = {
    "dataset": 1200,
    "semantic": 800,
    "tool_results": 2000,
    "evidence": 1500,
    "memory": 800,
    "documents": 2000,
    "conversation": 1000,
}

FLAGGED_RELIABILITY = 0.5


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...


class CharRatioTokenizer:
    """Conservative estimate: ceil(chars / ratio) + per-message overhead.

    ponytail: heuristic, not the model's tokenizer; 3.0 chars/token deliberately over-counts
    English (see BENCH-009 for the measured ratio). Swap in an exact tokenizer (llama.cpp
    /tokenize or the HF tokenizer) if budgets get tight.
    """

    def __init__(self, chars_per_token: float = 3.0, per_message_overhead: int = 4) -> None:
        self._ratio = chars_per_token
        self._overhead = per_message_overhead

    def count(self, text: str) -> int:
        return math.ceil(len(text) / self._ratio) + self._overhead


class ContextPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PartKind
    content: str
    trust: Literal["trusted", "untrusted"]
    priority: int = 0
    relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    provenance: list[str] = Field(default_factory=list)  # evidence/memory/chunk ids


class BuiltContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[Message]
    prompt_id: str
    prompt_version: str
    tokens: int
    dropped: list[str] = Field(default_factory=list)  # provenance ids (or kind) of dropped parts
    injection_flags: dict[str, list[str]] = Field(default_factory=dict)  # provenance -> flags


class ContextBuilder:
    def __init__(
        self,
        tokenizer: Tokenizer | None = None,
        *,
        kind_caps: dict[str, int] | None = None,
        heuristics: InjectionHeuristics | None = None,
    ) -> None:
        self._tokenizer = tokenizer or CharRatioTokenizer()
        self._caps = DEFAULT_KIND_CAPS if kind_caps is None else kind_caps
        self._heuristics = heuristics or InjectionHeuristics()

    def build(
        self,
        template: PromptTemplate,
        variables: dict[str, str],
        parts: list[ContextPart],
        budget: int,
    ) -> BuiltContext:
        system, user = template.render(**variables)
        count = self._tokenizer.count
        total = (count(system) if system else 0) + count(user)
        if total > budget:
            raise LLMFailure(
                LLMFailureReason.CONTEXT_TOO_LARGE,
                f"prompt {template.id} alone needs {total} tokens; budget is {budget}",
                retryable=False,
            )

        candidates: list[tuple[int, float, ContextPart, str, int]] = []
        flags_by_part: dict[str, list[str]] = {}
        for part in parts:
            rendered, weight = part.content, part.relevance
            if part.trust == "untrusted":
                flags = self._heuristics.scan(part.content)
                if flags:
                    weight *= FLAGGED_RELIABILITY
                    flags_by_part[",".join(part.provenance) or part.kind] = flags
                    log.warning(
                        "untrusted context flagged",
                        extra={
                            "fields": {
                                "kind": part.kind,
                                "provenance": part.provenance,
                                "flags": flags,
                            }
                        },
                    )
                rendered = wrap_untrusted(
                    part.content, kind=part.kind, sources=part.provenance, flags=flags
                )
            candidates.append((part.priority, weight, part, rendered, count(rendered)))
        candidates.sort(key=lambda c: (-c[0], -c[1]))

        used_by_kind: dict[str, int] = {}
        included: list[Message] = []
        dropped: list[str] = []
        for _, _, part, rendered, tokens in candidates:
            kind_used = used_by_kind.get(part.kind, 0)
            if total + tokens > budget or kind_used + tokens > self._caps.get(part.kind, budget):
                dropped.extend(part.provenance or [part.kind])
                continue
            total += tokens
            used_by_kind[part.kind] = kind_used + tokens
            included.append(Message(role="user", content=rendered, trust=part.trust))

        messages = [Message(role="system", content=system)] if system else []
        messages += [*included, Message(role="user", content=user)]
        return BuiltContext(
            messages=messages,
            prompt_id=template.id,
            prompt_version=template.version,
            tokens=total,
            dropped=dropped,
            injection_flags=flags_by_part,
        )
