"""Versioned thresholds and weights for data understanding (TASK-SCHEMA-002; design 13, 66).

Algorithm thresholds live in a versioned data file, not in env vars (design 66). The file version
is part of every ``engine_version`` string, so evidence records which thresholds produced a result.
JSON instead of YAML: the stdlib parses it and no new dependency is needed.
"""

from functools import cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data.domain import Ratio

CONFIG_DIR = Path(__file__).parent / "config"
DEFAULT_CONFIG = CONFIG_DIR / "understanding.v1.json"
Weight = Annotated[float, Field(ge=0.0)]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Abstention(_Cfg):
    """``tau``: minimum top score; ``delta``: minimum margin between the top two labels."""

    tau: Ratio
    delta: Ratio


class WeightedAbstention(Abstention):
    weights: dict[str, Weight]

    @model_validator(mode="after")
    def _weights_positive(self) -> Self:
        if sum(self.weights.values()) <= 0:
            raise ValueError("weights must contain at least one positive weight")
        return self


class KeyConfig(_Cfg):
    unique_min: Ratio
    null_max: Ratio
    reference_confidence: Ratio  # confidence of an entity only referenced by a non-unique id
    composite_max_columns: Annotated[int, Field(ge=2, le=3)]
    composite_candidate_limit: Annotated[int, Field(ge=2, le=12)]


class RelationshipConfig(_Cfg):
    inclusion_min: Ratio
    name_similarity_min: Ratio
    min_distinct_without_name: Annotated[int, Field(ge=1)]
    weights: dict[str, Weight]


class DimensionConfig(_Cfg):
    low_max: Annotated[int, Field(ge=1)]
    mid_max: Annotated[int, Field(ge=1)]


class DomainConfig(WeightedAbstention):
    lexicon_saturation: Annotated[float, Field(gt=0.0)]
    role_weights: dict[str, Weight]


class MappingConfig(WeightedAbstention):
    pack_weights: dict[str, Weight]  # "primary", "secondary", "generic"


class UnderstandingConfig(_Cfg):
    version: Annotated[str, Field(pattern=r"^understanding-config@\d+(\.\d+)*$")]
    semantic_type: WeightedAbstention
    time: WeightedAbstention
    key: KeyConfig
    relationship: RelationshipConfig
    dimension: DimensionConfig
    domain: DomainConfig
    mapping: MappingConfig


def load_config(path: Path = DEFAULT_CONFIG) -> UnderstandingConfig:
    return UnderstandingConfig.model_validate_json(path.read_text(encoding="utf-8"))


@cache
def default_config() -> UnderstandingConfig:
    return load_config()
