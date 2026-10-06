"""Understanding + domain classification contracts (TASK-SCHEMA-001; design 13-14, TRD 11.2-11.4).

Deviations:
- ``DomainStatus`` uses the UPPERCASE values from TASK-DOMAIN-005 (``KNOWN``, ``MULTI_DOMAIN``,
  ``AMBIGUOUS``, ``UNKNOWN``) instead of the design's lowercase ``known/multi/ambiguous/unknown``.
- ``DomainEvidence.kind`` adds ``user_override`` (design 14: an override "wins" with that evidence).
- ``Relationship`` serialises ``from``/``to``; the Python attribute is ``from_`` (keyword).
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data.domain import Ratio
from app.data.profiling.domain import PhysicalType, TimeGrain


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticType(StrEnum):
    IDENTIFIER = "identifier"
    DATETIME = "datetime"
    DATE_PART = "date_part"
    CURRENCY_AMOUNT = "currency_amount"
    QUANTITY = "quantity"
    COUNT = "count"
    PRICE = "price"
    RATE_OR_RATIO = "rate_or_ratio"
    PERCENTAGE = "percentage"
    CATEGORY = "category"
    BOOLEAN_FLAG = "boolean_flag"
    FREE_TEXT = "free_text"
    GEO_COUNTRY = "geo_country"
    GEO_REGION = "geo_region"
    GEO_CITY = "geo_city"
    POSTAL_CODE = "postal_code"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    UNKNOWN = "unknown"


Additivity = Literal["additive", "semi_additive", "non_additive"]


class ColumnUnderstanding(Contract):
    name: str
    source_name: str
    physical_type: PhysicalType
    semantic_type: SemanticType
    confidence: Ratio
    signals: list[str]  # human-readable signal descriptions that produced the decision


class Entity(Contract):
    name: str
    key_columns: Annotated[list[str], Field(min_length=1, max_length=3)]
    confidence: Ratio


class Relationship(Contract):
    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    from_: Annotated[str, Field(alias="from")]  # "table.column"
    to: str
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    inclusion: Ratio
    confidence: Ratio


class TimeDimension(Contract):
    column: str
    grain: TimeGrain | None
    regularity: Ratio
    confidence: Ratio


class Measure(Contract):
    column: str
    kind: Literal[
        "amount",
        "quantity",
        "count",
        "price",
        "unit_cost",
        "rate",
        "ratio",
        "percentage",
        "balance",
    ]
    additivity: Additivity
    unit: str | None  # e.g. "currency:INR", "pct"


class Dimension(Contract):
    column: str
    type: Literal["category", "geo", "boolean", "identifier"]
    cardinality_band: Literal["low", "mid", "high"]


class Understanding(Contract):
    schema_version: Literal["1.0"] = "1.0"
    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    engine_version: str
    columns: list[ColumnUnderstanding]
    entities: list[Entity]
    relationships: list[Relationship]
    time_dimensions: list[TimeDimension]  # ranked, preferred first
    measures: list[Measure]
    dimensions: list[Dimension]
    review_flags: list[str]  # e.g. MAPPING_AMBIGUOUS:net_amt

    @model_validator(mode="after")
    def _references(self) -> Self:
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("column names must be unique")
        used = [c for e in self.entities for c in e.key_columns]
        used += [t.column for t in self.time_dimensions]
        used += [m.column for m in self.measures] + [d.column for d in self.dimensions]
        missing = sorted(set(used) - set(names))
        if missing:
            raise ValueError(f"unknown columns referenced: {missing}")
        return self


class DomainStatus(StrEnum):
    KNOWN = "KNOWN"
    MULTI_DOMAIN = "MULTI_DOMAIN"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


class DomainEvidence(Contract):
    kind: Literal[
        "lexicon", "signature", "entity_graph", "statistical", "embedding", "llm", "user_override"
    ]
    detail: str
    weight: float


class DomainCandidate(Contract):
    domain: str  # pack id, e.g. "saas"
    score: Ratio
    evidence: list[DomainEvidence]


class DomainClassification(Contract):
    schema_version: Literal["1.0"] = "1.0"
    status: DomainStatus
    primary: str | None
    candidates: list[DomainCandidate]  # sorted by score, best first
    confidence: Ratio
    pack_versions: dict[str, str]

    @model_validator(mode="after")
    def _status_rules(self) -> Self:
        scores = [c.score for c in self.candidates]
        if scores != sorted(scores, reverse=True):
            raise ValueError("candidates must be sorted by descending score")
        if self.status is DomainStatus.UNKNOWN and self.primary is not None:
            raise ValueError("UNKNOWN has no primary domain")
        if self.status is DomainStatus.KNOWN and self.primary is None:
            raise ValueError("KNOWN requires a primary domain")
        if self.status is DomainStatus.MULTI_DOMAIN and len(self.candidates) < 2:
            raise ValueError("MULTI_DOMAIN requires at least two candidates")
        if self.primary is not None and self.primary not in {c.domain for c in self.candidates}:
            raise ValueError("primary must be one of the candidates")
        return self
