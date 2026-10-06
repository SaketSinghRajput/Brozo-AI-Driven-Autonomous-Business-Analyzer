"""TimeDimensionDetector (TASK-SCHEMA-007; TRD 11.3 "Time dimension").

Candidates are columns typed ``datetime`` or ``date_part``. Score = weighted(parse ratio,
regularity, name cue, completeness); candidates below tau are dropped, the rest ranked (preferred
first; ties keep column order). No candidate -> empty list ("no time dimension").
"""

import re
from collections.abc import Sequence

from app.data.profiling.domain import ColumnProfile, ProfileResult, TimeGrain
from app.data.understanding.config import UnderstandingConfig, default_config
from app.data.understanding.domain import ColumnUnderstanding, SemanticType, TimeDimension
from app.data.understanding.scoring import weighted_score
from app.data.understanding.text import tokens

_TIME_TOKENS = frozenset(
    {"date", "datetime", "timestamp", "time", "created", "month", "year", "week", "quarter",
     "day", "period", "start", "signup", "order"}
)  # fmt: skip
_SECONDARY = frozenset({"end", "updated", "modified", "closed", "cancelled", "expiry", "birth"})
_GRAIN_TOKENS: dict[str, TimeGrain] = {
    "year": "year",
    "fy": "year",
    "quarter": "quarter",
    "month": "month",
    "week": "week",
    "day": "day",
    "date": "day",
}
_YEAR_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class TimeDimensionDetector:
    def __init__(self, config: UnderstandingConfig | None = None) -> None:
        self._cfg = (config or default_config()).time

    def detect(
        self, profile: ProfileResult, columns: Sequence[ColumnUnderstanding]
    ) -> list[TimeDimension]:
        by_name = {c.name: c for c in profile.columns}
        ranked: list[tuple[float, int, TimeDimension]] = []
        for index, col in enumerate(columns):
            if col.semantic_type not in {SemanticType.DATETIME, SemanticType.DATE_PART}:
                continue
            p = by_name[col.name]
            regularity = _regularity(p, col.semantic_type)
            toks = set(tokens(col.name))
            name = 0.0 if not toks & _TIME_TOKENS else (0.4 if toks & _SECONDARY else 1.0)
            signals = {
                "parse": p.parse_success_ratio if p.temporal or p.numeric else 1.0,
                "regularity": regularity if regularity is not None else 0.5,
                "name": name,
                "completeness": 1.0 - p.null_ratio,
            }
            score = weighted_score(signals, self._cfg.weights)
            if score < self._cfg.tau:
                continue
            dim = TimeDimension(
                column=col.name,
                grain=_grain(p, col.name),
                regularity=round(regularity or 0.0, 4),
                confidence=round(score, 4),
            )
            ranked.append((score, index, dim))
        ranked.sort(key=lambda r: (-r[0], r[1]))
        return [dim for _, _, dim in ranked]


def _regularity(p: ColumnProfile, sem_type: SemanticType) -> float | None:
    if p.temporal is not None:
        return p.temporal.regularity
    if sem_type is SemanticType.DATE_PART and p.numeric is not None and p.cardinality:
        span = p.numeric.max - p.numeric.min + 1
        return min(1.0, p.cardinality / span) if span > 0 else None
    return None


def _grain(p: ColumnProfile, name: str) -> TimeGrain | None:
    if p.temporal is not None and p.temporal.granularity is not None:
        return p.temporal.granularity
    for tok in reversed(tokens(name)):
        if tok in _GRAIN_TOKENS:
            return _GRAIN_TOKENS[tok]
    if p.categorical and any(_YEAR_MONTH.match(v.value) for v in p.categorical.values):
        return "month"
    if p.numeric is not None and p.numeric.min >= 1900 and p.numeric.max <= 2100:
        return "year"
    return None
