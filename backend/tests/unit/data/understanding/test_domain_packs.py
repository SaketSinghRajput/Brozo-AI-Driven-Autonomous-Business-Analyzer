"""Domain pack format, loader validation and shipped packs (TASK-DOMAIN-001/002)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.data.domain_packs.loader import (
    PACKS_DIR,
    SCHEMA_PATH,
    PackValidationError,
    load_pack,
    load_packs,
    pack_json_schema,
)

RETAIL = json.loads((PACKS_DIR / "retail.v1.json").read_text(encoding="utf-8"))
Mutation = Callable[[dict[str, Any]], None]


def test_shipped_packs_load_and_cite_sources() -> None:
    packs = load_packs()
    assert set(packs) == {"generic", "retail", "saas", "agriculture"}
    assert packs["generic"].scorable is False
    for pack in packs.values():
        source_ids = {s.id for s in pack.sources}
        assert all(m.source in source_ids for m in pack.metrics)


def test_committed_json_schema_matches_model() -> None:
    committed = SCHEMA_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == pack_json_schema(), "regenerate domain_pack.schema.json"


def _write(tmp_path: Path, mutate: Mutation, name: str = "pack.json") -> Path:
    data = json.loads(json.dumps(RETAIL))
    mutate(data)
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _set(key: str, value: Any) -> Mutation:
    return lambda d: d.__setitem__(key, value)


def _metric_expr(expr: str) -> Mutation:
    return lambda d: d["metrics"][0].__setitem__("expr", expr)


BAD_EDGE = {"from": "Quantity", "to": "Order", "verb": "places"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["signatures"][0]["requires"].append("Nope"), "unknown concepts"),
        (lambda d: d["metrics"][0].__setitem__("source", "missing"), "unknown source"),
        (lambda d: d["concepts"].append(dict(d["concepts"][0])), "duplicate concept"),
        (lambda d: d["entity_graph"].append(BAD_EDGE), "not an entity concept"),
        (_metric_expr("sum(measure('Unit Price'))"), "non-additive"),
        (_metric_expr("__import__('os')"), "unknown function"),
        (_metric_expr("sum(measure('Ghost'))"), "unknown measure"),
        (_set("id", "Bad-Id"), "String should match pattern"),
        (_set("surprise", True), "Extra inputs are not permitted"),
        (_set("compatible_with", ["retail"]), "itself"),
    ],
)
def test_invalid_packs_are_rejected(tmp_path: Path, mutate: Mutation, message: str) -> None:
    with pytest.raises(PackValidationError, match=message):
        load_pack(_write(tmp_path, mutate))


def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PackValidationError, match=r"broken\.json"):
        load_pack(path)


def test_duplicate_pack_ids_rejected(tmp_path: Path) -> None:
    _write(tmp_path, lambda d: None, "a.json")
    _write(tmp_path, lambda d: None, "b.json")
    with pytest.raises(PackValidationError, match="duplicate pack id"):
        load_packs(tmp_path)


def test_unknown_compatible_pack_rejected(tmp_path: Path) -> None:
    _write(tmp_path, _set("compatible_with", ["marketing"]), "a.json")
    with pytest.raises(PackValidationError, match="unknown packs"):
        load_packs(tmp_path)
