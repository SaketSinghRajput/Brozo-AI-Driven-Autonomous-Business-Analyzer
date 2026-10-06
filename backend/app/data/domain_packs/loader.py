"""Domain pack loader + validation (TASK-DOMAIN-001).

``load_pack`` = JSON syntax -> ``DomainPack`` model (structure + cross-references) -> every metric
template parsed and type-checked against the pack's own concepts (templates may reference earlier
templates via ``metric('...')``). Any failure raises ``PackValidationError`` naming the file.
"""

import json
from functools import cache
from pathlib import Path

from pydantic import ValidationError

from app.data.domain_packs.model import Concept, DomainPack
from app.data.semantic.expression import ExpressionError
from app.data.semantic.typecheck import DictCatalog, Symbol, check_metric
from app.data.understanding.domain import SemanticType
from app.data.understanding.measures import classify_measure

PACKS_DIR = Path(__file__).parent / "packs"
SCHEMA_PATH = Path(__file__).parent / "domain_pack.schema.json"
_COUNTABLE = frozenset({SemanticType.QUANTITY, SemanticType.COUNT})


class PackValidationError(ValueError):
    pass


def load_pack(path: Path) -> DomainPack:
    try:
        pack = DomainPack.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise PackValidationError(f"{path.name}: {exc}") from exc
    try:
        _check_templates(pack)
    except ExpressionError as exc:
        raise PackValidationError(f"{path.name}: {exc.message}") from exc
    return pack


def load_packs(directory: Path = PACKS_DIR) -> dict[str, DomainPack]:
    packs: dict[str, DomainPack] = {}
    for path in sorted(directory.glob("*.json")):
        pack = load_pack(path)
        if pack.id in packs:
            raise PackValidationError(f"{path.name}: duplicate pack id {pack.id!r}")
        packs[pack.id] = pack
    unknown = {c for p in packs.values() for c in p.compatible_with} - set(packs)
    if unknown:
        raise PackValidationError(f"compatible_with references unknown packs {sorted(unknown)}")
    return packs


@cache
def default_packs() -> dict[str, DomainPack]:
    return load_packs()


def pack_json_schema() -> str:
    schema = DomainPack.model_json_schema(by_alias=True)
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def concept_symbol(concept: Concept) -> tuple[str, Symbol]:
    """How a pack concept is typed when referenced from an expression."""
    if concept.kind == "entity":
        return "dimension_key", Symbol("key")
    if concept.kind == "time":
        return "dimension", Symbol("time")
    if concept.kind == "dimension":
        return "dimension", Symbol("category")
    first = concept.semantic_types[0]
    classified = classify_measure(concept.name, first)
    additivity = classified[1] if classified else "additive"
    return "measure", Symbol("number", "count" if first in _COUNTABLE else None, additivity)


def _check_templates(pack: DomainPack) -> None:
    catalog: dict[tuple[str, str], Symbol] = {}
    for concept in pack.concepts:
        kind, symbol = concept_symbol(concept)
        catalog[(kind, concept.name)] = symbol
    for metric in pack.metrics:
        try:
            result = check_metric(metric.expr, DictCatalog(catalog))
        except ExpressionError as exc:
            raise ExpressionError(f"metric {metric.name!r}: {exc.message}") from exc
        catalog[("metric", metric.name)] = Symbol("number", result.unit, result.additivity)
