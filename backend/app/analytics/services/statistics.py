"""StatisticsService (TASK-ANALYSIS-011, design §22, TRD §15.2). Standard library only.

- ``describe``: n, missing, mean, sample std (n - 1), min, quartiles (inclusive method), max.
- ``correlate``: Pearson or Spearman (ranked Pearson) on pairwise-complete values, always with n
  and caveats; n < 3 or a constant input → ``undefined``. No p-value is reported.
- ``compare_groups``: per-group ``describe`` and each group's mean minus the overall mean. It is
  descriptive: no significance test is run, and the result says so.
"""

import statistics
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CORRELATION_CAVEAT = "correlation describes association only; it is not evidence of causation"
SMALL_SAMPLE = 30


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Description(Contract):
    n: Annotated[int, Field(ge=0)]
    missing: Annotated[int, Field(ge=0)]
    mean: float | None = None
    std: float | None = None  # sample standard deviation; None when n < 2
    min: float | None = None
    q1: float | None = None
    median: float | None = None
    q3: float | None = None
    max: float | None = None


class Correlation(Contract):
    method: Literal["pearson", "spearman"]
    r: float | None
    n: Annotated[int, Field(ge=0)]
    caveats: list[str]
    undefined: Literal["TOO_FEW_PAIRS", "CONSTANT_INPUT"] | None = None


class GroupComparison(Contract):
    groups: dict[str, Description]
    overall_mean: float | None
    difference_from_overall: dict[str, float | None]
    caveats: list[str]


def describe(values: Sequence[float | None]) -> Description:
    xs = [float(v) for v in values if v is not None]
    missing = len(values) - len(xs)
    if not xs:
        return Description(n=0, missing=missing)
    if len(xs) > 1:
        q1, median, q3 = statistics.quantiles(xs, n=4, method="inclusive")
    else:
        q1 = median = q3 = xs[0]
    return Description(
        n=len(xs),
        missing=missing,
        mean=statistics.fmean(xs),
        std=statistics.stdev(xs) if len(xs) > 1 else None,
        min=min(xs),
        q1=q1,
        median=median,
        q3=q3,
        max=max(xs),
    )


def correlate(
    a: Sequence[float | None],
    b: Sequence[float | None],
    method: Literal["pearson", "spearman"] = "pearson",
) -> Correlation:
    if len(a) != len(b):
        raise ValueError("series must have the same length")
    pairs = [
        (float(x), float(y)) for x, y in zip(a, b, strict=True) if x is not None and y is not None
    ]
    n = len(pairs)
    caveats = [CORRELATION_CAVEAT]
    if n < SMALL_SAMPLE:
        caveats.append(f"small sample (n={n}); the estimate is unstable")
    if n < 3:
        return Correlation(method=method, r=None, n=n, caveats=caveats, undefined="TOO_FEW_PAIRS")
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    if len(set(xs)) == 1 or len(set(ys)) == 1:
        return Correlation(method=method, r=None, n=n, caveats=caveats, undefined="CONSTANT_INPUT")
    kind: Literal["linear", "ranked"] = "ranked" if method == "spearman" else "linear"
    return Correlation(
        method=method, r=statistics.correlation(xs, ys, method=kind), n=n, caveats=caveats
    )


def compare_groups(groups: Mapping[str, Sequence[float | None]]) -> GroupComparison:
    described = {name: describe(values) for name, values in groups.items()}
    everything = [float(v) for values in groups.values() for v in values if v is not None]
    overall = statistics.fmean(everything) if everything else None
    differences = {
        name: (d.mean - overall) if d.mean is not None and overall is not None else None
        for name, d in described.items()
    }
    caveats = ["descriptive comparison only: no significance test was performed"]
    if any(d.n < SMALL_SAMPLE for d in described.values()):
        caveats.append(f"some groups have fewer than {SMALL_SAMPLE} observations")
    return GroupComparison(
        groups=described, overall_mean=overall, difference_from_overall=differences, caveats=caveats
    )
