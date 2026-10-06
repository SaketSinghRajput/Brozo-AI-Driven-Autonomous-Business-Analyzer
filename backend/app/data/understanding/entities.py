"""EntityDetector (TASK-SCHEMA-005; design 13, TRD 11.3).

- Primary key: a column with unique_ratio >= tau_key and null_ratio ~ 0 that is typed ``identifier``
  or carries a key token (id/key/code/number). Confidence rises with both cues.
- Referenced entity: an identifier column that is not unique (``customer_id`` on orders) names an
  entity the table points at; confidence = ``reference_confidence``.
- Composite key (<= 3 columns) is searched only when no single-column key exists and a
  ``DistinctCounter`` (row-level access) is supplied; profiles alone cannot prove combined
  uniqueness.
"""

from collections.abc import Sequence
from itertools import combinations
from math import prod
from typing import Protocol

from app.data.profiling.domain import ColumnProfile, ProfileResult
from app.data.understanding.config import UnderstandingConfig, default_config
from app.data.understanding.domain import ColumnUnderstanding, Entity, SemanticType
from app.data.understanding.text import singular, title, tokens

KEY_TOKENS = frozenset({"id", "key", "code", "number", "uuid", "guid", "sku", "identifier"})
GENERIC_TABLES = frozenset(
    {"data", "table", "sheet", "sheet1", "export", "dataset", "records", "record", "file", "rows"}
)
_COMPOSITE_TYPES = frozenset(
    {
        SemanticType.IDENTIFIER,
        SemanticType.DATETIME,
        SemanticType.DATE_PART,
        SemanticType.CATEGORY,
        SemanticType.BOOLEAN_FLAG,
    }
)


class DistinctCounter(Protocol):
    def count_distinct(self, columns: Sequence[str]) -> int:
        """Number of distinct value combinations over ``columns`` (exact)."""
        ...


def entity_name(column: str, table: str) -> str:
    """``customer_id`` -> ``Customer``; a bare ``id`` falls back to the (singular) table name."""
    stem = [t for t in tokens(column) if t not in KEY_TOKENS]
    if stem:
        return title(stem)
    table_tokens = [singular(t) for t in tokens(table)]
    if not table_tokens or set(table_tokens) <= {singular(g) for g in GENERIC_TABLES}:
        return "Record"
    return title(table_tokens)


class EntityDetector:
    def __init__(self, config: UnderstandingConfig | None = None) -> None:
        self._cfg = (config or default_config()).key

    def is_key(self, column: ColumnProfile) -> bool:
        return (
            column.unique_ratio >= self._cfg.unique_min
            and column.null_ratio <= self._cfg.null_max
            and column.physical_type not in {"float", "decimal", "datetime", "date", "bool"}
        )

    def detect(
        self,
        table: str,
        profile: ProfileResult,
        columns: Sequence[ColumnUnderstanding],
        counter: DistinctCounter | None = None,
    ) -> list[Entity]:
        by_name = {c.name: c for c in profile.columns}
        keys: list[tuple[float, str]] = []
        for col in columns:
            identifier = col.semantic_type is SemanticType.IDENTIFIER
            key_token = bool(KEY_TOKENS & set(tokens(col.name)))
            if self.is_key(by_name[col.name]) and (identifier or key_token):
                cue = 0.6 + 0.2 * identifier + 0.2 * key_token
                keys.append((round(by_name[col.name].unique_ratio * cue, 4), col.name))
        keys.sort(key=lambda k: -k[0])
        entities = [
            Entity(name=entity_name(name, table), key_columns=[name], confidence=confidence)
            for confidence, name in keys
        ]
        if not keys and counter is not None:
            composite = self._composite(profile, columns, counter)
            if composite:
                name = entity_name("", table)
                entities.append(Entity(name=name, key_columns=composite, confidence=0.9))
        key_set = {name for _, name in keys}
        entities += [
            Entity(
                name=entity_name(col.name, table),
                key_columns=[col.name],
                confidence=self._cfg.reference_confidence,
            )
            for col in columns
            if col.semantic_type is SemanticType.IDENTIFIER and col.name not in key_set
        ]
        return _dedupe(entities)

    def _composite(
        self,
        profile: ProfileResult,
        columns: Sequence[ColumnUnderstanding],
        counter: DistinctCounter,
    ) -> list[str] | None:
        by_name = {c.name: c for c in profile.columns}
        candidates = [
            c.name
            for c in columns
            if c.semantic_type in _COMPOSITE_TYPES and by_name[c.name].null_ratio == 0
        ]
        candidates.sort(key=lambda n: (-by_name[n].cardinality, n))
        candidates = candidates[: self._cfg.composite_candidate_limit]
        rows = profile.row_count
        for size in range(2, self._cfg.composite_max_columns + 1):
            for combo in combinations(candidates, size):
                if prod(by_name[n].cardinality for n in combo) < rows:
                    continue  # cannot be unique: fewer combinations than rows
                if counter.count_distinct(combo) == rows:
                    return list(combo)
        return None


def _dedupe(entities: list[Entity]) -> list[Entity]:
    seen: set[str] = set()
    out = []
    for e in entities:
        if e.name not in seen:
            seen.add(e.name)
            out.append(e)
    return out
