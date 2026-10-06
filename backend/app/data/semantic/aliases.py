"""Alias / synonym resolution (TASK-SEM-005; design 15 step 1-2).

Headers and aliases are compared as token sequences after abbreviation expansion
(``total_amt`` -> ``total amount``) and singularisation. Similarity ladder:

| relation                                    | score                          |
|---------------------------------------------|--------------------------------|
| same token sequence                         | 1.0                            |
| same token set (order differs)              | 0.95                           |
| alias is a contiguous phrase of the header  | 0.6 + 0.4 * len(alias)/len(hdr) |
| alias tokens all in header (not contiguous) | 0.5 + 0.3 * len(alias)/len(hdr) |
| header is a phrase of the alias             | 0.5                            |
| otherwise                                   | 0.5 * Jaccard                  |

Confirmed prior mappings (Semantic Memory) outrank every alias match.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.data.domain_packs.model import Concept
from app.data.understanding.text import contains_phrase, jaccard, singular, tokens

PRIOR_SCORE = 1.0


def _norm(name: str) -> tuple[str, ...]:
    return tuple(singular(t) for t in tokens(name))


def alias_similarity(header: str, alias: str) -> float:
    h, a = _norm(header), _norm(alias)
    if not h or not a:
        return 0.0
    if h == a:
        return 1.0
    if set(h) == set(a):
        return 0.95
    if contains_phrase(h, a):
        return 0.6 + 0.4 * len(a) / len(h)
    if set(a) <= set(h):
        return 0.5 + 0.3 * len(a) / len(h)
    if contains_phrase(a, h):
        return 0.5
    return 0.5 * jaccard(h, a)


def best_alias(header: str, aliases: Iterable[str]) -> tuple[float, str | None]:
    best: tuple[float, str | None] = (0.0, None)
    for alias in aliases:
        score = alias_similarity(header, alias)
        if score > best[0]:
            best = (score, alias)
    return best


@dataclass(frozen=True)
class AliasMatch:
    concept: Concept
    score: float
    alias: str | None  # None when the match came from a confirmed prior mapping
    from_prior: bool = False


def resolve(
    header: str, concepts: Iterable[Concept], prior: Mapping[str, str] | None = None
) -> list[AliasMatch]:
    """All concepts ``header`` may denote, best first. A confirmed prior mapping wins outright."""
    pool = list(concepts)
    confirmed = (prior or {}).get(header)
    if confirmed is not None:
        hit = next((c for c in pool if c.name == confirmed), None)
        if hit is not None:
            return [AliasMatch(hit, PRIOR_SCORE, None, from_prior=True)]
    matches = []
    for concept in pool:
        score, alias = best_alias(header, [concept.name, *concept.aliases])
        if score > 0:
            matches.append(AliasMatch(concept, round(score, 4), alias))
    matches.sort(key=lambda m: (-m.score, m.concept.name))
    return matches
