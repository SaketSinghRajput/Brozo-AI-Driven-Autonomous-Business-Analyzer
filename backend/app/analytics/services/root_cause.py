"""RootCauseService (TASK-ANALYSIS-014, design §43, TRD §28). Contributing factors, not causes.

Pipeline for an observed change of ``metric`` between the comparison and the current range:
1. Outcome: metric total in both ranges (``compare_to``) → percent change (a fraction).
2. Candidate factors: members of each candidate dimension (default: every category/geo/boolean
   dimension of the semantic model).
3. Decomposition: ``contribution_to_change`` per dimension; the dimension's concentration is the
   share of absolute change carried by its top member. Optional price-volume-mix over the most
   concentrated dimension when a volume metric is given.
4. Temporal precedence: over ``history_periods`` periods, did the member already move in the
   same direction (by at least half as much) in the period before the comparison period?
   → "preceded", else "coincident".
5. Association: Pearson correlation of the member series with the total series (n + caveats).
6. Ranking: score = |share of change| (primary) + 0.1 if preceded + 0.1 * |r| (secondary).

Every finding has ``relation_type`` "contribution"; "causation" is refused by the contract until
a CausalEvidence object exists (P3). All numbers come from compiled queries, which are recorded.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.analytics.domain import CompareTo, MetricRequest, ResultTable, SemanticName
from app.analytics.services.contribution import (
    ChangeResult,
    PriceVolumeMix,
    contribution_to_change,
    price_volume_mix,
)
from app.analytics.services.growth import GrowthResult, pct_change
from app.analytics.services.statistics import Correlation, correlate
from app.analytics.sql.periods import Grain, add_months, period_start
from app.analytics.sql.resolver import SemanticResolver
from app.analytics.sql.runner import MetricQueryRunner
from app.core.errors import SchemaError
from app.data.domain import Cell
from app.data.semantic.domain import SemanticMeasure
from app.planner.domain import Filter, TimeSpec

CAUSAL_CERTAINTY = (
    "Not established. Findings are contributions to the change and associations; no causal "
    "method or experiment was applied."
)
PRECEDENCE_BONUS, ASSOCIATION_WEIGHT, MEMBERS_PER_DIMENSION = 0.1, 0.1, 3
Temporal = Literal["preceded", "coincident", "not_assessed"]
MemberSeries = dict[tuple[str, date], float | None]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RootCauseRequest(Contract):
    metric: SemanticName
    current_start: date
    current_end: date
    compare_to: CompareTo = "previous_period"
    time_dimension: SemanticName | None = None
    dimensions: Annotated[list[SemanticName], Field(max_length=8)] = []
    filters: Annotated[list[Filter], Field(max_length=20)] = []
    grain: Grain = "month"
    history_periods: Annotated[int, Field(ge=2, le=24)] = 6
    volume_metric: SemanticName | None = None
    top_k: Annotated[int, Field(ge=1, le=20)] = 5

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.current_start > self.current_end:
            raise ValueError("current_start must not be after current_end")
        return self


class FactorFinding(Contract):
    factor: str  # "Category=Electronics"
    dimension: str
    member: str
    relation_type: Literal["contribution", "association", "correlation", "causation"]
    magnitude: float  # change of the metric carried by this member
    unit: str | None
    share_pct: float | None  # share of the total change, a fraction (0.25 = 25%)
    temporal: Temporal
    association: Correlation
    score: float
    evidence_ids: list[str] = []  # the tool call's evidence record covers every finding
    caveats: list[str]

    @model_validator(mode="after")
    def _no_causation(self) -> Self:
        if self.relation_type == "causation":
            raise ValueError("causation requires CausalEvidence, which is not available (P3)")
        return self


class DimensionSummary(Contract):
    dimension: str
    concentration_pct: float | None  # |top member change| / sum |member changes| (fraction)
    top_member: str | None
    change: ChangeResult


class RootCauseResult(Contract):
    metric: str
    unit: str | None
    current_range: tuple[date, date]
    compare_range: tuple[date, date]
    before: float | None
    after: float | None
    delta: float | None
    outcome: GrowthResult
    dimensions: list[DimensionSummary]
    findings: list[FactorFinding]
    price_volume_mix: PriceVolumeMix | None = None
    causal_certainty: str = CAUSAL_CERTAINTY
    caveats: list[str]
    queries: list[dict[str, JsonValue]]


def _number(cell: Cell) -> float | None:
    return float(cell) if isinstance(cell, int | float) and not isinstance(cell, bool) else None


def _label(cell: Cell) -> str:
    return "(null)" if cell is None else str(cell)


def _shift(day: date, grain: Grain, periods: int) -> date:
    if grain == "day":
        return day + timedelta(days=periods)
    if grain == "week":
        return day + timedelta(days=7 * periods)
    return add_months(day, periods * {"month": 1, "quarter": 3, "year": 12}[grain])


def _delta(series: MemberSeries, key: str, start: date, end: date) -> float | None:
    a, b = series.get((key, start)), series.get((key, end))
    return None if a is None and b is None else (b or 0.0) - (a or 0.0)


class RootCauseService:
    def __init__(self, runner: MetricQueryRunner) -> None:
        self._runner = runner
        self._resolver = SemanticResolver(runner.model)
        self._queries: list[dict[str, JsonValue]] = []

    def _run(self, request: MetricRequest) -> ResultTable:
        run = self._runner.run(request)
        self._queries.append(run.record())
        return run.table

    def _request(
        self, req: RootCauseRequest, metric: str, dims: Sequence[str], start: date, compare: bool
    ) -> MetricRequest:
        return MetricRequest(
            metric=metric,
            dimensions=list(dims),
            filters=req.filters,
            time=TimeSpec(grain=req.grain, start=start, end=req.current_end),
            time_dimension=req.time_dimension,
            compare_to=req.compare_to if compare else None,
            limit=10_000,
        )

    def explain(self, req: RootCauseRequest) -> RootCauseResult:
        self._queries = []
        target = self._resolver.metric(req.metric)
        names = req.dimensions or [
            d.name
            for d in self._runner.model.dimensions
            if d.type in {"category", "geo", "boolean"}
        ]
        if not names:
            raise SchemaError("no candidate dimensions to explain the change with")
        dims = [self._resolver.dimension(d).name for d in names]
        outcome = self._runner.run(self._request(req, req.metric, [], req.current_start, True))
        self._queries.append(outcome.record())
        compare_range = outcome.compiled.compare_range
        if compare_range is None:
            raise SchemaError("a comparison range is required")
        row = outcome.table.rows[0] if outcome.table.rows else [None, None]
        after, before = _number(row[0]), _number(row[1])
        unit = outcome.table.columns[0].unit
        caveats = []
        summed = isinstance(target, SemanticMeasure) and target.default_agg in {"sum", "count"}
        if target.additivity == "non_additive" or (
            isinstance(target, SemanticMeasure) and not summed
        ):
            caveats.append("the metric is not additive: member changes need not sum to the total")
        summaries = [self._summary(req, dim) for dim in dims]
        findings = self._findings(req, summaries, unit, compare_range[0])
        return RootCauseResult(
            metric=target.name,
            unit=unit,
            current_range=(req.current_start, req.current_end),
            compare_range=compare_range,
            before=before,
            after=after,
            delta=after - before if after is not None and before is not None else None,
            outcome=pct_change(before, after),
            dimensions=sorted(summaries, key=lambda s: (-(s.concentration_pct or 0), s.dimension)),
            findings=findings,
            price_volume_mix=self._pvm(req, summaries) if req.volume_metric else None,
            caveats=caveats,
            queries=self._queries,
        )

    def _summary(self, req: RootCauseRequest, dim: str) -> DimensionSummary:
        table = self._run(self._request(req, req.metric, [dim], req.current_start, True))
        change = contribution_to_change(
            {_label(r[0]): _number(r[2]) for r in table.rows},
            {_label(r[0]): _number(r[1]) for r in table.rows},
        )
        total_abs = sum(abs(i.delta) for i in change.items)
        top = change.items[0] if change.items else None
        return DimensionSummary(
            dimension=dim,
            concentration_pct=abs(top.delta) / total_abs if top and total_abs else None,
            top_member=top.key if top else None,
            change=change,
        )

    def _findings(
        self,
        req: RootCauseRequest,
        summaries: Sequence[DimensionSummary],
        unit: str | None,
        compare_start: date,
    ) -> list[FactorFinding]:
        t0 = period_start(compare_start, req.grain)
        first = _shift(t0, req.grain, -(req.history_periods - 1))
        totals = {
            date.fromisoformat(str(r[0])): _number(r[1])
            for r in self._run(self._request(req, req.metric, [], first, False)).rows
        }
        periods = sorted(totals)
        total_series = [totals[p] for p in periods]
        t_minus, t1 = _shift(t0, req.grain, -1), period_start(req.current_start, req.grain)
        findings: list[FactorFinding] = []
        for summary in summaries:
            history = self._run(self._request(req, req.metric, [summary.dimension], first, False))
            members: MemberSeries = {
                (_label(r[0]), date.fromisoformat(str(r[1]))): _number(r[2]) for r in history.rows
            }
            for item in summary.change.items[:MEMBERS_PER_DIMENSION]:
                assoc = correlate([members.get((item.key, p)) for p in periods], total_series)
                prior, now = (
                    _delta(members, item.key, t_minus, t0),
                    _delta(members, item.key, t0, t1),
                )
                temporal: Temporal = "not_assessed"
                if prior is not None and now:
                    preceded = prior * now > 0 and abs(prior) >= 0.5 * abs(now)
                    temporal = "preceded" if preceded else "coincident"
                score = abs(item.share_pct or 0) + ASSOCIATION_WEIGHT * abs(assoc.r or 0)
                score += PRECEDENCE_BONUS if temporal == "preceded" else 0
                findings.append(
                    FactorFinding(
                        factor=f"{summary.dimension}={item.key}",
                        dimension=summary.dimension,
                        member=item.key,
                        relation_type="contribution",
                        magnitude=item.delta,
                        unit=unit,
                        share_pct=item.share_pct,
                        temporal=temporal,
                        association=assoc,
                        score=score,
                        caveats=[
                            "a contribution to the change, not a proven cause",
                            *summary.change.caveats,
                        ],
                    )
                )
        findings.sort(key=lambda f: (-f.score, f.factor))
        return findings[: req.top_k]

    def _pvm(self, req: RootCauseRequest, summaries: Sequence[DimensionSummary]) -> PriceVolumeMix:
        if req.volume_metric is None:
            raise SchemaError("price-volume-mix needs a volume metric")
        best = max(summaries, key=lambda s: s.concentration_pct or 0).dimension
        value = self._run(self._request(req, req.metric, [best], req.current_start, True))
        volume = self._run(self._request(req, req.volume_metric, [best], req.current_start, True))
        volumes = {_label(r[0]): (_number(r[1]), _number(r[2])) for r in volume.rows}
        before: dict[str, tuple[float, float]] = {}
        after: dict[str, tuple[float, float]] = {}
        for row in value.rows:
            key = _label(row[0])
            v_after, v_before = _number(row[1]), _number(row[2])
            q_after, q_before = volumes.get(key, (None, None))
            if v_before is not None and q_before is not None:
                before[key] = (v_before, q_before)
            if v_after is not None and q_after is not None:
                after[key] = (v_after, q_after)
        return price_volume_mix(before, after)
