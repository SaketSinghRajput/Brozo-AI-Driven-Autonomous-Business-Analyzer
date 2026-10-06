"""RankingService (TASK-ANALYSIS-010, design §22). Ties are explicit, never silently broken.

Methods (values ordered descending by default):
- ``competition`` ("1224"): tied items share the best rank; the next rank skips.
- ``dense`` ("1223"): tied items share a rank; the next rank does not skip.
- ``row`` ("1234"): unique ranks; ties are broken by key ascending and still flagged ``tied``.
``top_n`` keeps items whose rank is <= n, so with ``competition``/``dense`` a tie at the cutoff
can return more than n items; ``ties_at_cutoff`` says so. Null values are excluded and counted.
"""

from collections import Counter
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

RankMethod = Literal["competition", "dense", "row"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RankedItem(Contract):
    key: str
    value: float
    rank: Annotated[int, Field(ge=1)]
    tied: bool


class RankingResult(Contract):
    method: RankMethod
    descending: bool
    items: list[RankedItem]
    ties_at_cutoff: bool
    excluded_nulls: Annotated[int, Field(ge=0)]


def rank(
    items: Sequence[tuple[str, float | None]],
    method: RankMethod = "competition",
    descending: bool = True,
    top_n: int | None = None,
) -> RankingResult:
    if top_n is not None and top_n < 1:
        raise ValueError("top_n must be >= 1")
    present = [(k, v) for k, v in items if v is not None]
    sign = -1 if descending else 1
    ordered = sorted(present, key=lambda kv: (sign * kv[1], kv[0]))
    counts = Counter(v for _, v in ordered)
    ranked: list[RankedItem] = []
    dense = competition = 0
    previous: float | None = None
    for position, (key, value) in enumerate(ordered, start=1):
        if value != previous:
            dense += 1
            competition = position
            previous = value
        number = {"competition": competition, "dense": dense, "row": position}[method]
        ranked.append(RankedItem(key=key, value=value, rank=number, tied=counts[value] > 1))
    kept = ranked if top_n is None else [r for r in ranked if r.rank <= top_n]
    cutoff_tie = False
    if top_n is not None and kept:
        beyond = len(ranked) > len(kept) and ranked[len(kept)].value == kept[-1].value
        cutoff_tie = beyond or len(kept) > top_n
    return RankingResult(
        method=method,
        descending=descending,
        items=kept,
        ties_at_cutoff=cutoff_tie,
        excluded_nulls=len(items) - len(present),
    )
