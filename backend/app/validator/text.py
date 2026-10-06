"""Answer text helpers shared by the validators: rendering, sentences, direction words.

Segments are rendered into one string in which every number segment becomes ``PLACEHOLDER``
(not a digit, not a word character), so wording around structured numbers can be read with the
same rules as wording around numbers typed into free text.

A direction word applies to a number when it directly precedes it ("fell 14.2%", "rose by
about 3%") or directly follows it ("a 14.2% decline"). "fell to 4.8M" states a level, not a
change, so no direction applies.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.validator.domain import AnswerSegment, NumberSegment

Direction = Literal["up", "down"]
PLACEHOLDER = ""

_UP = (
    "increased increase increases increasing rose rise rises risen rising grew grow grows grown "
    "growing growth up gained gain gains climbed jumped surged"
)
_DOWN = (
    "decreased decrease decreases decreasing declined decline declines declining fell fall falls "
    "fallen falling dropped drop drops down shrank shrunk lost slipped plunged contracted reduced"
)
_WORDS = {w: "up" for w in _UP.split()} | {w: "down" for w in _DOWN.split()}
_DIRECTION_WORD = re.compile(r"\b(" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")\b")
_GAP = re.compile(
    r"\s*(?:by|of)?\s*(?:about|approximately|roughly|nearly|almost|around|over|some)?\s*"
)
_AFTER_UP = re.compile(r"\s*(?:higher|more|increase|rise|growth|gain|jump)\b")
_AFTER_DOWN = re.compile(r"\s*(?:lower|less|decline|decrease|drop|fall|loss|reduction)\b")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    offsets: tuple[int, ...]  # start of each segment in ``text``


def render(segments: Sequence[AnswerSegment]) -> Rendered:
    parts: list[str] = []
    offsets: list[int] = []
    position = 0
    for seg in segments:
        part = PLACEHOLDER if isinstance(seg, NumberSegment) else seg.text
        offsets.append(position)
        parts.append(part)
        position += len(part)
    return Rendered("".join(parts), tuple(offsets))


def sentence_start(text: str, pos: int) -> int:
    start = 0
    for m in _SENTENCE_END.finditer(text, 0, pos):
        start = m.end()
    return start


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text) if s.strip()]


def direction_at(text: str, start: int, end: int) -> Direction | None:
    """The direction word governing the number at ``text[start:end]``, if any."""
    before = text[sentence_start(text, start) : start].lower()
    words = list(_DIRECTION_WORD.finditer(before))
    if words and _GAP.fullmatch(before[words[-1].end() :]):
        return "up" if _WORDS[words[-1].group()] == "up" else "down"
    after = text[end:].lower()
    if _AFTER_UP.match(after):
        return "up"
    if _AFTER_DOWN.match(after):
        return "down"
    return None


def contradicts(direction: Direction | None, value: float) -> bool:
    return (direction == "up" and value < 0) or (direction == "down" and value > 0)
