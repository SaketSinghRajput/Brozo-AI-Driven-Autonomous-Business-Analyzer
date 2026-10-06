"""RelationshipDetector (TASK-SCHEMA-006; design 13, TRD 11.3).

A foreign-key candidate A.col -> B.key needs, in order:
1. B.key is a single-column key of B (unique, no nulls);
2. type compatibility (same physical family: integer vs string never join);
3. a name cue (token Jaccard, or A.col mentions B's singular table name) OR, for string codes only,
   enough distinct values that an accidental full overlap is implausible;
4. inclusion |distinct(A) & distinct(B)| / |distinct(A)| >= tau_fk (exact, on full distinct sets).

Numeric surrogate keys (1..N) overlap by accident, so they need a name cue. ``Relationship`` has no
evidence field, so findings carry the evidence strings next to the contract object.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.data.profiling.domain import ProfileResult
from app.data.understanding.config import UnderstandingConfig, default_config
from app.data.understanding.domain import ColumnUnderstanding, Relationship, SemanticType
from app.data.understanding.entities import EntityDetector
from app.data.understanding.scoring import weighted_score
from app.data.understanding.text import jaccard, singular, tokens

_FAMILY = {"int": "int", "string": "str", "mixed": "str"}
_JOINABLE = frozenset({SemanticType.IDENTIFIER, SemanticType.CATEGORY, SemanticType.UNKNOWN})


@dataclass(frozen=True)
class TableSnapshot:
    name: str
    profile: ProfileResult
    columns: Sequence[ColumnUnderstanding]
    distinct: Callable[[str], frozenset[str]]  # all distinct non-null values of a column


@dataclass(frozen=True)
class RelationshipFinding:
    relationship: Relationship
    evidence: list[str]


class RelationshipDetector:
    def __init__(self, config: UnderstandingConfig | None = None) -> None:
        cfg = config or default_config()
        self._cfg = cfg.relationship
        self._keys = EntityDetector(cfg)

    def detect(
        self, source: TableSnapshot, targets: Sequence[TableSnapshot]
    ) -> list[RelationshipFinding]:
        found = []
        src_profiles = {c.name: c for c in source.profile.columns}
        for col in source.columns:
            family = _FAMILY.get(col.physical_type)
            if family is None or col.semantic_type not in _JOINABLE:
                continue
            for target in targets:
                if target.name == source.name:
                    continue
                for key in self._target_keys(target):
                    finding = self._check(source, col.name, target, key, family)
                    if finding:
                        unique = src_profiles[col.name].unique_ratio >= 0.999
                        found.append(_with_cardinality(finding, unique))
        found.sort(key=lambda f: (-f.relationship.confidence, f.relationship.from_))
        return found

    def _target_keys(self, target: TableSnapshot) -> list[str]:
        return [c.name for c in target.profile.columns if self._keys.is_key(c)]

    def _check(
        self, source: TableSnapshot, column: str, target: TableSnapshot, key: str, family: str
    ) -> RelationshipFinding | None:
        key_profile = next(c for c in target.profile.columns if c.name == key)
        if _FAMILY.get(key_profile.physical_type) != family:
            return None
        src_tokens = tokens(column)
        name_sim = jaccard(src_tokens, tokens(key))
        table_stem = {singular(t) for t in tokens(target.name)}
        if table_stem and table_stem <= set(src_tokens):
            name_sim = max(name_sim, 0.8)
        values = source.distinct(column)
        named = name_sim >= self._cfg.name_similarity_min
        plausible = family == "str" and len(values) >= self._cfg.min_distinct_without_name
        if not values or not (named or plausible):
            return None
        inclusion = len(values & target.distinct(key)) / len(values)
        if inclusion < self._cfg.inclusion_min:
            return None
        signals = {"inclusion": inclusion, "name": name_sim, "target_is_key": 1.0}
        confidence = round(weighted_score(signals, self._cfg.weights), 4)
        relationship = Relationship(
            from_=f"{source.name}.{column}",
            to=f"{target.name}.{key}",
            cardinality="many_to_one",
            inclusion=round(inclusion, 4),
            confidence=confidence,
        )
        evidence = [
            f"inclusion {inclusion:.3f} of {len(values)} distinct values of {column}",
            f"{target.name}.{key} is unique (key)",
            f"name similarity {name_sim:.2f}",
            f"type family {family}",
        ]
        return RelationshipFinding(relationship, evidence)


def _with_cardinality(finding: RelationshipFinding, source_unique: bool) -> RelationshipFinding:
    if not source_unique:
        return finding
    rel = finding.relationship.model_copy(update={"cardinality": "one_to_one"})
    return RelationshipFinding(rel, finding.evidence)
