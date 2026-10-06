"""DimensionDetector (TASK-SCHEMA-009; TRD 11.3 "Dimension").

category -> category; low/mid-cardinality free text -> category (e.g. product names);
geo_* / postal_code -> geo; boolean_flag -> boolean; identifiers that are not the table's own key
-> identifier (grouping by customer, product, ...). Bands: low <= low_max < mid <= mid_max < high.
"""

from collections.abc import Sequence
from typing import Literal

from app.data.profiling.domain import ProfileResult
from app.data.understanding.config import UnderstandingConfig, default_config
from app.data.understanding.domain import ColumnUnderstanding, Dimension, SemanticType

T = SemanticType
DimensionType = Literal["category", "geo", "boolean", "identifier"]
Band = Literal["low", "mid", "high"]
GEO = frozenset({T.GEO_COUNTRY, T.GEO_REGION, T.GEO_CITY, T.POSTAL_CODE})


class DimensionDetector:
    def __init__(self, config: UnderstandingConfig | None = None) -> None:
        self._cfg = (config or default_config()).dimension

    def band(self, cardinality: int) -> Band:
        if cardinality <= self._cfg.low_max:
            return "low"
        return "mid" if cardinality <= self._cfg.mid_max else "high"

    def detect(
        self,
        profile: ProfileResult,
        columns: Sequence[ColumnUnderstanding],
        primary_keys: frozenset[str] = frozenset(),
    ) -> list[Dimension]:
        by_name = {c.name: c for c in profile.columns}
        dims = []
        for col in columns:
            band = self.band(by_name[col.name].cardinality)
            kind = _kind(col, band, primary_keys)
            if kind is not None:
                dims.append(Dimension(column=col.name, type=kind, cardinality_band=band))
        return dims


def _kind(
    col: ColumnUnderstanding, band: Band, primary_keys: frozenset[str]
) -> DimensionType | None:
    t = col.semantic_type
    if t is T.CATEGORY or (t is T.FREE_TEXT and band != "high"):
        return "category"
    if t in GEO:
        return "geo"
    if t is T.BOOLEAN_FLAG:
        return "boolean"
    if t is T.IDENTIFIER and col.name not in primary_keys:
        return "identifier"
    return None
