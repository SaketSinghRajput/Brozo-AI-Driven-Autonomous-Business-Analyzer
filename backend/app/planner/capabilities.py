"""CapabilityEvaluator (TASK-ANALYSIS-002; design 17, TRD 14.2, CLAUDE.md section 12.1).

Evaluates the versioned capability rules against ``DataFacts``. Every failing top-level
requirement yields one ``Unavailability`` whose message starts with "Insufficient data:" and names
the specific missing requirement with the numbers involved (threshold and what the data has), so
the user sees e.g. "Insufficient data: forecasting needs at least 12 monthly periods of history;
the data covers 3." An analysis is available only when no requirement fails. Deterministic: the
same facts and rules always give the same report (analyses in rule-file order).
"""

from collections.abc import Callable, Mapping

from app.data.understanding.text import tokens
from app.planner.config import AnalysisConfig, default_analysis_config, default_rules
from app.planner.domain import (
    CapabilityCheck,
    CapabilityReport,
    CapabilityRuleSet,
    ReasonCode,
    Requirement,
    Unavailability,
)
from app.planner.facts import DataFacts

C = CapabilityCheck
Params = Mapping[str, object]
Check = Callable[[DataFacts, Params, str | None], str | None]  # -> missing requirement or None

LABELS = {
    "kpi": "KPI cards",
    "trend": "trend analysis",
    "forecast": "forecasting",
    "anomaly_detection": "anomaly detection",
    "customer_segmentation": "customer segmentation",
    "churn_analysis": "churn analysis",
    "cohort_analysis": "cohort analysis",
    "rca": "root-cause analysis",
}
WOULD_ENABLE: dict[ReasonCode, str | None] = {
    ReasonCode.NO_TIME_DIMENSION: "a date column (e.g. order or transaction date)",
    ReasonCode.IRREGULAR_TIME: "dates recorded at a regular interval (e.g. daily or monthly)",
    ReasonCode.INSUFFICIENT_HISTORY: "a longer history covering more periods",
    ReasonCode.TOO_MANY_MISSING_PERIODS: "data for the missing periods",
    ReasonCode.NO_MEASURE: "a numeric amount or quantity column",
    ReasonCode.NO_DIMENSION: "a category column such as region, product or channel",
    ReasonCode.NO_CUSTOMER_IDENTIFIER: "a customer id column",
    ReasonCode.NO_RETENTION_HISTORY: (
        "subscription status or start/end dates per customer, or repeated dated activity per "
        "customer"
    ),
    ReasonCode.NO_TARGET: "choosing the column to predict",
    ReasonCode.INSUFFICIENT_ENTITIES: "more distinct customers",
    ReasonCode.INSUFFICIENT_ROWS: "more rows",
    ReasonCode.NO_NUMERIC_FEATURES: "more numeric columns",
    ReasonCode.LOW_MODEL_QUALITY: None,
    ReasonCode.SIMULATION_UNSUPPORTED: None,
}
_ADJECTIVE = {"month": "monthly", "quarter": "quarterly", "year": "yearly"}
_STATUS_TOKENS = frozenset({"status", "churn", "churned", "cancel", "cancelled", "canceled"})
_END_TOKENS = frozenset({"end", "cancel", "cancelled", "canceled", "churn", "churned"})


def _num(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, got {value!r}")
    return float(value)


def _time_dimension(f: DataFacts, p: Params, entity: str | None) -> str | None:
    return None if f.time else "a date or time column (none was recognised)"


def _time_regularity(f: DataFacts, p: Params, entity: str | None) -> str | None:
    minimum = _num(p["min"])
    if f.time is None:
        return f"regularly spaced dates (regularity >= {minimum:.2f}); there is no time column"
    if f.time.regularity >= minimum:
        return None
    return (
        f"regularly spaced dates (regularity >= {minimum:.2f}); "
        f"column '{f.time.column}' has {f.time.regularity:.2f}"
    )


def _history_periods(f: DataFacts, p: Params, entity: str | None) -> str | None:
    if f.time is None:
        return "a time column to measure the history"
    raw = p["min"]
    grain = f.time.planning_grain
    minimum = _num(raw[grain] if isinstance(raw, Mapping) else raw)
    if f.time.periods >= minimum:
        return None
    return (
        f"at least {minimum:g} {_ADJECTIVE[grain]} periods of history; "
        f"the data covers {f.time.periods}"
    )


def _measure(f: DataFacts, p: Params, entity: str | None) -> str | None:
    allowed = p.get("additivity_in")
    kinds = frozenset(allowed) if isinstance(allowed, list) else None
    if any(kinds is None or m.additivity in kinds for m in f.model.measures):
        return None
    return "a numeric measure" + (" that can be summed" if kinds else "")


def _dimension(f: DataFacts, p: Params, entity: str | None) -> str | None:
    limit = _num(p.get("max_cardinality", float("inf")))
    if f.categorical_dimensions(limit):
        return None
    return f"a categorical dimension with at most {limit:g} values"


def _entity(f: DataFacts, p: Params, entity: str | None) -> str | None:
    name = str(p["name"])
    return None if f.entity(name) else f"a {name} identifier column"


def _entity_count(f: DataFacts, p: Params, entity: str | None) -> str | None:
    minimum = _num(p["min"])
    found = f.entity(entity) if entity else next(iter(f.model.entities), None)
    label = entity or "entity"
    column = f.column(found.key.split(",")[0]) if found else None
    if column is None:  # a missing entity is reported once, by the rule's `entity` check
        return None if entity else "an entity identifier column to count"
    if column.cardinality >= minimum:
        return None
    return f"at least {minimum:g} distinct {label} values; the data has {column.cardinality}"


def _row_count(f: DataFacts, p: Params, entity: str | None) -> str | None:
    minimum = _num(p["min"])
    rows = f.profile.row_count
    return None if rows >= minimum else f"at least {minimum:g} rows; the data has {rows}"


def _numeric_features(f: DataFacts, p: Params, entity: str | None) -> str | None:
    minimum = _num(p["min"])
    count = len(f.model.measures)
    if count >= minimum:
        return None
    return f"at least {minimum:g} numeric columns; the data has {count}"


def _target(f: DataFacts, p: Params, entity: str | None) -> str | None:
    if f.target is None:
        return "a chosen target column to predict (none was chosen)"
    return None if f.column(f.target) else f"the target column '{f.target}', which does not exist"


def _subscription_status(f: DataFacts, p: Params, entity: str | None) -> str | None:
    for d in f.model.dimensions:
        names = set(tokens(d.column)) | set(tokens(d.name))
        if (d.type == "category" and names & _STATUS_TOKENS) or (
            d.type == "time" and names & _END_TOKENS
        ):
            return None
    return "a subscription status or end date column"


def _activity_history(f: DataFacts, p: Params, entity: str | None) -> str | None:
    customer = f.entity("Customer")
    column = f.column(customer.key.split(",")[0]) if customer else None
    if column is not None and f.time is not None and column.unique_ratio < 1.0:
        return None
    return "repeated, dated activity per customer"


CHECKS: dict[CapabilityCheck, Check] = {
    C.TIME_DIMENSION: _time_dimension,
    C.TIME_REGULARITY: _time_regularity,
    C.HISTORY_PERIODS: _history_periods,
    C.MEASURE: _measure,
    C.DIMENSION: _dimension,
    C.ENTITY: _entity,
    C.ENTITY_COUNT: _entity_count,
    C.ROW_COUNT: _row_count,
    C.NUMERIC_FEATURES: _numeric_features,
    C.TARGET: _target,
    C.SUBSCRIPTION_STATUS: _subscription_status,
    C.ACTIVITY_HISTORY_PER_CUSTOMER: _activity_history,
}


class CapabilityEvaluator:
    def __init__(
        self, rules: CapabilityRuleSet | None = None, config: AnalysisConfig | None = None
    ) -> None:
        self._rules = rules or default_rules()
        self._config = config or default_analysis_config()

    @property
    def rules_version(self) -> str:
        return self._rules.rules_version

    def evaluate(self, facts: DataFacts) -> CapabilityReport:
        available: list[str] = []
        unavailable: list[Unavailability] = []
        for name, rule in self._rules.analyses.items():
            entity = next(
                (str(r.params["name"]) for r in rule.requires if r.check is C.ENTITY), None
            )
            label = LABELS.get(name, name.replace("_", " "))
            misses = []
            for requirement in rule.requires:
                missing = self._check(requirement, facts, entity)
                code = requirement.reason_code
                if missing is None or code is None:  # code is set on top-level rules
                    continue
                misses.append(
                    Unavailability(
                        analysis=name,
                        reason_code=code,
                        message=f"Insufficient data: {label} needs {missing}.",
                        would_enable=WOULD_ENABLE[code],
                    )
                )
            if misses:
                unavailable += misses
            else:
                available.append(name)
        return CapabilityReport(
            dataset_version_id=facts.dataset_version_id,
            semantic_model_version=facts.model.version,
            rules_version=self._rules.rules_version,
            available=available,
            unavailable=unavailable,
        )

    def _check(self, requirement: Requirement, facts: DataFacts, entity: str | None) -> str | None:
        if requirement.check is C.ANY_OF:
            problems = [self._check(child, facts, entity) for child in requirement.any_of]
            if any(p is None for p in problems):
                return None
            return "one of: " + "; or ".join(str(p) for p in problems)
        params = {k: self._config.resolve(v) for k, v in requirement.params.items()}
        return CHECKS[requirement.check](facts, params, entity)
