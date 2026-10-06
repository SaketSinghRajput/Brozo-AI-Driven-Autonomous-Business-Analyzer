"""Profiling contracts (TASK-PROFILE-001; design.md section 12, TRD 11.1).

Sub-models (``NumericStats``, ``TopK``, ...) spell out the fields listed in the design comments.
Invariants enforced: ratios in [0, 1]; ``cardinality <= row_count - nulls``; unique column names;
``sampled`` implies ``sample_size``.
"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.data.domain import Ratio

PhysicalType = Literal["int", "float", "decimal", "bool", "datetime", "date", "string", "mixed"]
UnitHint = Literal["currency", "percent"]  # ingest-time symbol seen; percent values are fractions
TimeGrain = Literal["second", "minute", "hour", "day", "week", "month", "quarter", "year"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NumericStats(Contract):
    min: float
    max: float
    mean: float
    median: float
    std: float
    var: float
    skew: float | None
    quantiles: dict[str, float]  # "q01" .. "q99"
    zero_ratio: Ratio
    neg_ratio: Ratio


class TopKValue(Contract):
    value: str  # masked when the column is PII-like
    count: Annotated[int, Field(ge=0)]
    frequency: Ratio


class TopK(Contract):
    values: list[TopKValue]
    masked: bool


class TemporalStats(Contract):
    min: AwareDatetime
    max: AwareDatetime
    granularity: TimeGrain | None  # mode of successive differences
    regularity: Ratio  # share of differences equal to the mode
    gaps: Annotated[int, Field(ge=0)]
    tz_mixed: bool


class TextStats(Contract):
    min_length: Annotated[int, Field(ge=0)]
    max_length: Annotated[int, Field(ge=0)]
    mean_length: Annotated[float, Field(ge=0)]
    pattern_classes: dict[str, Ratio]  # digits / alpha / mixed histogram
    regex_hit_ratios: dict[str, Ratio]  # email, url, currency, percent, date


class OutlierSummary(Contract):
    method: Literal["iqr", "mad"]
    count: Annotated[int, Field(ge=0)]
    sample_indices: list[int]  # indices only; never values of PII-like columns


class ColumnProfile(Contract):
    name: str
    physical_type: PhysicalType
    parse_success_ratio: Ratio
    null_ratio: Ratio
    unique_ratio: Ratio
    cardinality: Annotated[int, Field(ge=0)]
    numeric: NumericStats | None = None
    categorical: TopK | None = None
    temporal: TemporalStats | None = None
    text: TextStats | None = None
    outliers: OutlierSummary | None = None
    unit_hint: UnitHint | None = None


class CorrelationPair(Contract):
    a: str
    b: str
    method: Literal["pearson", "spearman"]
    coefficient: Annotated[float, Field(ge=-1.0, le=1.0)]
    n: Annotated[int, Field(ge=2)]


class ProfileResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    engine_version: str
    row_count: Annotated[int, Field(ge=0)]
    duplicate_rows: Annotated[int, Field(ge=0)]
    columns: list[ColumnProfile]
    correlations: list[CorrelationPair]
    sampled: bool
    sample_size: Annotated[int, Field(ge=1)] | None

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("column names must be unique")
        if self.duplicate_rows > self.row_count:
            raise ValueError("duplicate_rows cannot exceed row_count")
        for c in self.columns:
            non_null = self.row_count - round(c.null_ratio * self.row_count)
            if c.cardinality > non_null:
                raise ValueError(f"column {c.name!r}: cardinality exceeds non-null rows")
        if self.sampled != (self.sample_size is not None):
            raise ValueError("sample_size must be set exactly when sampled is true")
        known = set(names)
        if any(p.a not in known or p.b not in known for p in self.correlations):
            raise ValueError("correlations must reference profiled columns")
        return self
