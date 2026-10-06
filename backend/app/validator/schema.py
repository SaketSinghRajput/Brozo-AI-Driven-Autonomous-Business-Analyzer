"""SchemaValidator (TASK-VAL-002; TRD 26 schema check).

References come from two places, because ``ValidationRequest`` has no reference list:
1. tool arguments recorded in cited evidence (``metric``, ``dimensions``, ``column``, ``table``,
   ``filters`` ...), checked against the semantic model of that evidence's dataset version;
2. identifiers the answer text quotes in backticks (```churn_rate```), checked against every
   loaded model.
Unknown names are hard issues. A reference that cannot be checked because no semantic model is
available is soft (SCHEMA_UNVERIFIED), unless the request pinned a semantic model version,
in which case the missing model is hard. Names compare case-insensitively.
"""

import re
from collections.abc import Iterator, Mapping

from pydantic import JsonValue

from app.data.semantic.domain import SemanticModel
from app.validator.domain import (
    CaveatSegment,
    EvidenceBundle,
    TextSegment,
    ValidationIssue,
    ValidationRequest,
    cited_ids,
)

Kind = str  # "metric" | "dimension" | "column" | "table"
_ARGUMENT_KINDS: dict[str, Kind] = {
    "metric": "metric",
    "metrics": "metric",
    "measure": "metric",
    "measures": "metric",
    "dimension": "dimension",
    "dimensions": "dimension",
    "group_by": "dimension",
    "breakdown": "dimension",
    "column": "column",
    "columns": "column",
    "time_column": "column",
    "date_column": "column",
    "table": "table",
    "tables": "table",
}
_FILTER_KEYS = ("column", "dimension", "field")
_BACKTICKED = re.compile(r"`([^`\s]{1,128})`")


def _names(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        yield from (v for v in value if isinstance(v, str))


def references(arguments: Mapping[str, JsonValue]) -> list[tuple[Kind, str]]:
    refs = [
        (kind, name)
        for key, value in arguments.items()
        if (kind := _ARGUMENT_KINDS.get(key))
        for name in _names(value)
    ]
    filters = arguments.get("filters")
    if isinstance(filters, dict):
        refs += [("column", name) for name in filters]
    elif isinstance(filters, list):
        for f in filters:
            if isinstance(f, dict):
                refs += [("column", n) for k in _FILTER_KEYS for n in _names(f.get(k))]
    return refs


def vocabulary(model: SemanticModel) -> dict[Kind, frozenset[str]]:
    metrics = {m.name for m in model.metrics} | {m.name for m in model.measures}
    metrics |= {alias for m in model.measures for alias in m.aliases}
    columns = {d.column for d in model.dimensions} | {m.column for m in model.measures}
    columns |= {e.key for e in model.entities}
    dimensions = {d.name for d in model.dimensions} | {d.column for d in model.dimensions}
    tables = {e.source_table for e in model.entities}
    every = metrics | columns | dimensions | tables | {e.name for e in model.entities}
    raw = {"metric": metrics, "dimension": dimensions, "column": every, "table": tables}
    return {kind: frozenset(n.casefold() for n in names) for kind, names in raw.items()}


class SchemaValidator:
    name = "schema"
    version = "1.0.0"

    def __init__(self, models: Mapping[str, SemanticModel]) -> None:
        """``models``: dataset_version_id -> the semantic model the answer was built on."""
        self._vocab = {dsv: vocabulary(m) for dsv, m in models.items()}

    async def check(self, req: ValidationRequest, ev: EvidenceBundle) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for evidence_id in cited_ids(req):
            evidence = ev.get(evidence_id)
            refs = references(evidence.arguments) if evidence else []
            if evidence is None or not refs:
                continue
            vocab = self._vocab.get(evidence.dataset_version_id or "")
            if vocab is None:
                issues.append(self._no_model(req, evidence_id))
                continue
            issues += [
                _unknown(kind, name, evidence_id)
                for kind, name in refs
                if name.casefold() not in vocab[kind]
            ]
        return issues + self._quoted(req)

    def _no_model(self, req: ValidationRequest, target: str) -> ValidationIssue:
        if req.semantic_model_version is not None:
            detail = f"semantic model version {req.semantic_model_version} not found"
            return ValidationIssue(
                code="UNKNOWN_SEMANTIC_MODEL", severity="hard", target=target, detail=detail
            )
        detail = "no semantic model available to check the referenced names"
        return ValidationIssue(
            code="SCHEMA_UNVERIFIED", severity="soft", target=target, detail=detail
        )

    def _quoted(self, req: ValidationRequest) -> list[ValidationIssue]:
        known = frozenset().union(*(v["column"] for v in self._vocab.values()))
        issues: list[ValidationIssue] = []
        for idx, seg in enumerate(req.segments or []):
            if not isinstance(seg, TextSegment | CaveatSegment):
                continue
            for name in _BACKTICKED.findall(seg.text):
                target = f"segments[{idx}]"
                if not self._vocab:
                    issues.append(self._no_model(req, target))
                elif name.casefold() not in known:
                    issues.append(_unknown("column", name, target))
        return issues


def _unknown(kind: Kind, name: str, target: str) -> ValidationIssue:
    return ValidationIssue(
        code=f"UNKNOWN_{kind.upper()}",
        severity="hard",
        target=target,
        detail=f"{kind} {name!r} does not exist in the semantic model",
    )
