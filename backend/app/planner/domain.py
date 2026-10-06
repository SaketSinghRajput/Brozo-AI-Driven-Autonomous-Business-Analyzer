"""Planner contracts (TASK-ANALYSIS-001, TASK-ANALYSIS-003; design 17, TRD 14.2-14.3).

Capability rules format spec (closes the TASK-ANALYSIS-001 design gap)
=====================================================================
A rule file is versioned data (YAML or JSON) validated by ``CapabilityRuleSet``::

    schema_version: "1.0"
    rules_version: "0.1.0"               # semver; recorded in evidence config_versions
    analyses:
      forecast:                          # analysis name: ^[a-z][a-z0-9_]*$
        requires:                        # ALL must hold (AND)
          - {check: time_dimension, reason_code: NO_TIME_DIMENSION}
          - {check: time_regularity, params: {min: 0.8}, reason_code: IRREGULAR_TIME}
          - {check: measure, params: {additivity_in: [additive, semi_additive]},
             reason_code: NO_MEASURE}
          - {check: history_periods, params: {min: config.forecast.min_periods},
             reason_code: INSUFFICIENT_HISTORY}
      churn_analysis:
        requires:
          - {check: entity, params: {name: Customer}, reason_code: NO_CUSTOMER_IDENTIFIER}
          - check: any_of                # at least one child holds (OR)
            reason_code: NO_RETENTION_HISTORY
            any_of: [{check: subscription_status}, {check: activity_history_per_customer}]

Semantics: the evaluator checks requirements in order. Every failing top-level requirement
yields one ``Unavailability(analysis, reason_code, message, would_enable)``. So an analysis can
list several reasons, and it is available only when none fail. Children of ``any_of`` carry no
reason code. A string param value starting with ``config.`` is a reference into the versioned
analysis config and is resolved by the evaluator. Unknown checks, unknown param keys, missing
reason codes and unknown reason codes are rejected when the file is loaded.

The difference from the TRD 14.2 sketch is that each requirement names its own reason code,
instead of keeping one ``reason_codes`` list per analysis, so a failure maps to exactly one code.
``time_dimension {regularity_min}`` is split into ``time_dimension`` and ``time_regularity``
because they fail with different codes.

Other deviations: ``AnalysisPlan`` uses ``plan_id`` (TRD 14.3 and the dashboard spec) instead of
the design's ``analysis_id``, and it keeps the design's ``steps``/``type`` naming rather than the
TRD's ``nodes``/``op``. ``TimeSpec.range`` is flattened into ``start``/``end``.
"""

from datetime import date
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.data.profiling.domain import TimeGrain


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReasonCode(StrEnum):
    """Why an analysis is unavailable. Shown to users via ``Unavailability`` and ``unavailable``
    dashboard widgets (typed result, not an error: TRD 35 ANALYSIS_UNAVAILABLE)."""

    NO_TIME_DIMENSION = "NO_TIME_DIMENSION"  # no usable date/datetime column
    IRREGULAR_TIME = "IRREGULAR_TIME"  # time regularity below the rule threshold
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"  # too few periods (e.g. < 2 seasonal cycles)
    TOO_MANY_MISSING_PERIODS = "TOO_MANY_MISSING_PERIODS"  # gaps beyond tolerance (design 25)
    NO_MEASURE = "NO_MEASURE"  # no numeric measure with a compatible additivity
    NO_DIMENSION = "NO_DIMENSION"  # nothing to group/compare by
    NO_CUSTOMER_IDENTIFIER = "NO_CUSTOMER_IDENTIFIER"  # no customer entity/key
    NO_RETENTION_HISTORY = "NO_RETENTION_HISTORY"  # no subscription status or activity history
    NO_TARGET = "NO_TARGET"  # supervised task without a target column
    INSUFFICIENT_ENTITIES = "INSUFFICIENT_ENTITIES"  # too few customers/products/groups
    INSUFFICIENT_ROWS = "INSUFFICIENT_ROWS"  # below the minimum rows for the method
    NO_NUMERIC_FEATURES = "NO_NUMERIC_FEATURES"  # multivariate method lacks numeric features
    LOW_MODEL_QUALITY = "LOW_MODEL_QUALITY"  # e.g. segmentation quality below threshold
    SIMULATION_UNSUPPORTED = "SIMULATION_UNSUPPORTED"  # driver absent from the semantic model
    ANALYSIS_FAILED = "ANALYSIS_FAILED"  # a planned step failed or was skipped (GAPS-5c)


class CapabilityCheck(StrEnum):
    TIME_DIMENSION = "time_dimension"
    TIME_REGULARITY = "time_regularity"
    HISTORY_PERIODS = "history_periods"
    MEASURE = "measure"
    DIMENSION = "dimension"
    ENTITY = "entity"
    ENTITY_COUNT = "entity_count"
    ROW_COUNT = "row_count"
    NUMERIC_FEATURES = "numeric_features"
    TARGET = "target"
    SUBSCRIPTION_STATUS = "subscription_status"
    ACTIVITY_HISTORY_PER_CUSTOMER = "activity_history_per_customer"
    ANY_OF = "any_of"


_C = CapabilityCheck
ALLOWED_PARAMS: dict[CapabilityCheck, frozenset[str]] = {
    _C.TIME_DIMENSION: frozenset(),
    _C.TIME_REGULARITY: frozenset({"min"}),
    _C.HISTORY_PERIODS: frozenset({"min"}),
    _C.MEASURE: frozenset({"additivity_in"}),
    _C.DIMENSION: frozenset({"max_cardinality"}),
    _C.ENTITY: frozenset({"name"}),
    _C.ENTITY_COUNT: frozenset({"min"}),
    _C.ROW_COUNT: frozenset({"min"}),
    _C.NUMERIC_FEATURES: frozenset({"min"}),
    _C.TARGET: frozenset(),
    _C.SUBSCRIPTION_STATUS: frozenset(),
    _C.ACTIVITY_HISTORY_PER_CUSTOMER: frozenset(),
    _C.ANY_OF: frozenset(),
}

ParamValue = float | int | bool | str | list[str]
AnalysisName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class Requirement(Contract):
    check: CapabilityCheck
    params: dict[str, ParamValue] = {}
    reason_code: ReasonCode | None = None  # required at top level (see CapabilityRule)
    any_of: list[Requirement] = []

    @model_validator(mode="after")
    def _shape(self) -> Self:
        unknown = set(self.params) - ALLOWED_PARAMS[self.check]
        if unknown:
            raise ValueError(f"check {self.check}: unknown params {sorted(unknown)}")
        if (self.check is CapabilityCheck.ANY_OF) != bool(self.any_of):
            raise ValueError("any_of children are required exactly for check 'any_of'")
        if any(child.reason_code is not None for child in self.any_of):
            raise ValueError("any_of children must not carry a reason_code")
        return self


class CapabilityRule(Contract):
    description: str | None = None
    requires: Annotated[list[Requirement], Field(min_length=1)]

    @model_validator(mode="after")
    def _reason_codes(self) -> Self:
        if any(r.reason_code is None for r in self.requires):
            raise ValueError("every top-level requirement needs a reason_code")
        return self


class CapabilityRuleSet(Contract):
    schema_version: Literal["1.0"] = "1.0"
    rules_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    analyses: dict[AnalysisName, CapabilityRule]


class Unavailability(Contract):
    analysis: str
    reason_code: ReasonCode
    message: str
    would_enable: str | None = None  # e.g. "subscription start/end dates per customer"


class CapabilityReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    semantic_model_version: Annotated[int, Field(ge=1)]
    rules_version: str
    available: list[str]
    unavailable: list[Unavailability]
    # U-4: post-run outcomes. Analyses the rules allow (still in ``available``) whose latest
    # dashboard run refused them with a typed reason (e.g. LOW_MODEL_QUALITY: no segmentation
    # passed the quality gate). Empty before the first run.
    attempted: list[Unavailability] = []

    @model_validator(mode="after")
    def _disjoint(self) -> Self:
        both = set(self.available) & {u.analysis for u in self.unavailable}
        if both:
            raise ValueError(f"analyses both available and unavailable: {sorted(both)}")
        return self


FilterScalar = str | int | float | bool | date


class Filter(Contract):
    dimension: str
    op: Literal["eq", "in", "between", "gte", "lte"]
    value: FilterScalar | list[FilterScalar]

    @model_validator(mode="after")
    def _arity(self) -> Self:
        is_list = isinstance(self.value, list)
        if self.op == "in" and not (is_list and self.value):
            raise ValueError("'in' needs a non-empty list")
        if self.op == "between" and not (isinstance(self.value, list) and len(self.value) == 2):
            raise ValueError("'between' needs exactly two values")
        if self.op in {"eq", "gte", "lte"} and is_list:
            raise ValueError(f"'{self.op}' needs a single value")
        return self


class TimeSpec(Contract):
    grain: TimeGrain
    start: date | None = None
    end: date | None = None
    calendar: Literal["gregorian"] = "gregorian"

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must not be after end")
        return self


class AnalysisIntent(Contract):
    schema_version: Literal["1.0"] = "1.0"
    objective: Literal[
        "overview",
        "metric_value",
        "trend",
        "compare",
        "rank",
        "explain_change",
        "forecast",
        "anomalies",
        "segments",
        "predict",
        "simulate",
        "document_lookup",
    ]
    metrics: list[str] = []
    dimensions: list[str] = []
    filters: list[Filter] = []
    time: TimeSpec | None = None
    source: Literal["system_default", "user_question"]
    # Optional (B1/B4): which time dimension the period applies to (None: the model's first),
    # and what a comparison compares with (None: the previous period).
    time_dimension: str | None = None
    compare_to: Literal["previous_period", "same_period_last_year"] | None = None


class AnalysisStep(Contract):
    id: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")]  # "S1"
    type: str  # "aggregate", "growth_rate", "forecast", ...
    tool: Annotated[str, Field(pattern=r"^[a-z]+(\.[a-z_]+)+$")]  # "analytics.metric_query"
    args: dict[str, JsonValue] = {}  # checked against the tool input schema by PlanValidator
    depends_on: list[str] = []
    expected_output: str
    validation: list[Literal["schema", "numeric", "model", "logic"]] = []


class AnalysisPlan(Contract):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: Annotated[str, Field(pattern=r"^pl_\w+$")]
    version: Annotated[int, Field(ge=1)]
    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    semantic_model_version: Annotated[int, Field(ge=1)]
    intent: AnalysisIntent
    steps: list[AnalysisStep]
    unavailable: list[Unavailability] = []

    @model_validator(mode="after")
    def _dag(self) -> Self:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        for s in self.steps:
            missing = set(s.depends_on) - set(ids)
            if missing:
                raise ValueError(f"step {s.id}: unknown dependencies {sorted(missing)}")
        try:
            TopologicalSorter({s.id: s.depends_on for s in self.steps}).prepare()
        except CycleError as exc:
            raise ValueError(f"steps contain a dependency cycle: {exc.args[1]}") from None
        return self
