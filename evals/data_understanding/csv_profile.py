"""TEST-ONLY CSV -> ProfileResult helper for the data-understanding eval harness (TASK-EVAL-001).

NOT the production profiler (that is TASK-PROFILE-*, app/data/profiling services). This is a tiny
deterministic stdlib implementation that fills the ProfileResult contract well enough to drive the
understanding / domain / semantic detectors over evals/fixtures. Do not import it from app/.
"""

import csv
import math
import re
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path

from app.data.profiling.domain import (
    ColumnProfile,
    NumericStats,
    PhysicalType,
    ProfileResult,
    TemporalStats,
    TextStats,
    TimeGrain,
    TopK,
    TopKValue,
)

_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+(\.\d+)?([eE]-?\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?")
_REGEX_HITS = {
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.\w{2,}$"),
    "url": re.compile(r"^(https?://|www\.)"),
    "currency": re.compile(r"^[$€£₹]\s?\d|\d\s?(INR|USD|EUR|GBP)$"),
    "percent": re.compile(r"^-?\d+(\.\d+)?\s?%$"),
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}/\d{1,2}/\d{2,4}$"),
}
_PHYSICAL_TESTS: list[tuple[PhysicalType, Callable[[str], bool]]] = [
    ("bool", lambda v: v.lower() in {"true", "false"}),
    ("int", lambda v: bool(_INT.match(v))),
    ("float", lambda v: bool(_FLOAT.match(v))),
    ("date", lambda v: bool(_DATE.match(v))),
    ("datetime", lambda v: bool(_DATETIME.match(v))),
]
TOP_K = 10


@dataclass(frozen=True)
class ProfiledTable:
    name: str
    profile: ProfileResult
    distinct: dict[str, frozenset[str]]  # column -> distinct non-null raw values
    rows: list[dict[str, str]]


def profile_csv(path: Path, dataset_version_id: str) -> ProfiledTable:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    names = list(rows[0].keys()) if rows else []
    columns = [_profile_column(n, [r[n] for r in rows]) for n in names]
    duplicates = len(rows) - len({tuple(r.values()) for r in rows})
    profile = ProfileResult(
        dataset_version_id=dataset_version_id,
        engine_version="eval-csv-profile@1",
        row_count=len(rows),
        duplicate_rows=duplicates,
        columns=columns,
        correlations=[],
        sampled=False,
        sample_size=None,
    )
    distinct = {n: frozenset(r[n] for r in rows if r[n] != "") for n in names}
    return ProfiledTable(path.stem, profile, distinct, rows)


def _physical(values: list[str]) -> tuple[PhysicalType, float]:
    for kind, test in _PHYSICAL_TESTS:
        ok = sum(1 for v in values if test(v)) / len(values) if values else 0.0
        if ok >= 0.95:
            return kind, ok
    return "string", 1.0


def _profile_column(name: str, raw: list[str]) -> ColumnProfile:
    values = [v for v in raw if v != ""]
    kind, parse_ratio = _physical(values)
    counts = Counter(values)
    card = len(counts)
    numeric = temporal = text = categorical = None
    if kind in {"int", "float"}:
        numeric = _numeric([float(v) for v in values if _FLOAT.match(v)])
    if kind in {"date", "datetime"}:
        temporal = _temporal(values)
    if kind == "string":
        text = _text(values)
    if kind in {"string", "bool", "int"} and values:
        top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_K]
        categorical = TopK(
            values=[TopKValue(value=v, count=c, frequency=c / len(values)) for v, c in top],
            masked=False,
        )
    return ColumnProfile(
        name=name,
        physical_type=kind,
        parse_success_ratio=parse_ratio,
        null_ratio=(len(raw) - len(values)) / (len(raw) or 1),
        unique_ratio=card / len(values) if values else 0.0,
        cardinality=card,
        numeric=numeric,
        categorical=categorical,
        temporal=temporal,
        text=text,
    )


def _numeric(xs: list[float]) -> NumericStats | None:
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    mean = statistics.fmean(xs)
    std = statistics.pstdev(xs) if n > 1 else 0.0
    skew = (sum((x - mean) ** 3 for x in xs) / n) / std**3 if std > 0 else None

    def q(p: float) -> float:
        return xs[min(n - 1, max(0, math.ceil(p * n) - 1))]

    return NumericStats(
        min=xs[0],
        max=xs[-1],
        mean=mean,
        median=statistics.median(xs),
        std=std,
        var=std**2,
        skew=skew,
        quantiles={f"q{int(p * 100):02d}": q(p) for p in (0.01, 0.25, 0.5, 0.75, 0.99)},
        zero_ratio=sum(1 for x in xs if x == 0) / n,
        neg_ratio=sum(1 for x in xs if x < 0) / n,
    )


def _grain(days: int) -> TimeGrain | None:
    bands: list[tuple[int, int, TimeGrain]] = [
        (1, 1, "day"),
        (7, 7, "week"),
        (28, 31, "month"),
        (89, 92, "quarter"),
        (365, 366, "year"),
    ]
    return next((g for lo, hi, g in bands if lo <= days <= hi), None)


def _temporal(values: list[str]) -> TemporalStats | None:
    days = sorted({date.fromisoformat(v[:10]) for v in values if _DATE.match(v[:10])})
    if not days:
        return None
    diffs = [(b - a).days for a, b in pairwise(days)]
    mode = Counter(diffs).most_common(1)[0][0] if diffs else 0
    return TemporalStats(
        min=datetime.combine(days[0], datetime.min.time(), UTC),
        max=datetime.combine(days[-1], datetime.min.time(), UTC),
        granularity=_grain(mode) if diffs else None,
        regularity=sum(1 for d in diffs if d == mode) / len(diffs) if diffs else 1.0,
        gaps=sum(1 for d in diffs if d > mode),
        tz_mixed=False,
    )


def _pattern(v: str) -> str:
    if v.isdigit():
        return "digits"
    return "alpha" if v.replace(" ", "").isalpha() else "mixed"


def _text(values: list[str]) -> TextStats:
    n = len(values) or 1
    lengths = [len(v) for v in values] or [0]
    classes = Counter(_pattern(v) for v in values)
    return TextStats(
        min_length=min(lengths),
        max_length=max(lengths),
        mean_length=statistics.fmean(lengths),
        pattern_classes={k: classes.get(k, 0) / n for k in ("digits", "alpha", "mixed")},
        regex_hit_ratios={
            k: sum(1 for v in values if rx.search(v)) / n for k, rx in _REGEX_HITS.items()
        },
    )
