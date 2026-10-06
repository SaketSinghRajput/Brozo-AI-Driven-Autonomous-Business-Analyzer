"""SemanticResolver (TASK-SQL-003, design §21): user/LLM names → semantic objects.

Matching is case-, space-, underscore- and hyphen-insensitive over names, measure aliases and the
physical column name. A name that maps to two different objects of the same kind is ambiguous.
Errors never echo the requested text; they list close semantic names as suggestions (those come
from the semantic model, not from the caller).
"""

import difflib
import re
from collections.abc import Iterable

from app.core.errors import SchemaError
from app.data.semantic.domain import (
    SemanticDimension,
    SemanticEntity,
    SemanticMeasure,
    SemanticMetric,
    SemanticModel,
)

_SEPARATORS = re.compile(r"[\s_\-]+")


def normalize(name: str) -> str:
    return " ".join(p for p in _SEPARATORS.split(name.casefold().strip()) if p)


class _Index[T]:
    def __init__(self, kind: str, entries: Iterable[tuple[T, str, list[str]]]) -> None:
        self.kind = kind
        self._by_key: dict[str, T] = {}
        self._canonical: dict[str, str] = {}
        self._ambiguous: set[str] = set()
        for obj, name, keys in entries:
            for key in {normalize(k) for k in [name, *keys] if k.strip()}:
                existing = self._by_key.get(key)
                if existing is not None and existing is not obj:
                    self._ambiguous.add(key)
                self._by_key[key] = obj
                self._canonical.setdefault(key, name)

    def find(self, name: str) -> T | None:
        key = normalize(name)
        if key in self._ambiguous:
            raise SchemaError(f"ambiguous {self.kind} name; use the exact semantic name")
        return self._by_key.get(key)

    def suggestions(self, name: str) -> list[str]:
        close = difflib.get_close_matches(normalize(name), list(self._by_key), n=3, cutoff=0.5)
        return list(dict.fromkeys(self._canonical[k] for k in close))

    def get(self, name: str) -> T:
        found = self.find(name)
        if found is None:
            hint = self.suggestions(name)
            suffix = f"; did you mean: {', '.join(hint)}" if hint else ""
            raise SchemaError(f"unknown {self.kind}{suffix}", details={"suggestions": hint})
        return found


class SemanticResolver:
    def __init__(self, model: SemanticModel) -> None:
        self.model = model
        self._metrics = _Index[SemanticMetric]("metric", ((m, m.name, []) for m in model.metrics))
        self._measures = _Index[SemanticMeasure](
            "measure", ((m, m.name, [*m.aliases, m.column]) for m in model.measures)
        )
        self._dimensions = _Index[SemanticDimension](
            "dimension", ((d, d.name, [d.column]) for d in model.dimensions)
        )
        self._entities = _Index[SemanticEntity]("entity", ((e, e.name, []) for e in model.entities))

    def metric(self, name: str) -> SemanticMetric | SemanticMeasure:
        """A metric by name, else a measure (aggregated with its default aggregation)."""
        found = self._metrics.find(name) or self._measures.find(name)
        if found is not None:
            return found
        hint = self._metrics.suggestions(name) + self._measures.suggestions(name)
        hint = list(dict.fromkeys(hint))[:3]
        suffix = f"; did you mean: {', '.join(hint)}" if hint else ""
        raise SchemaError(f"unknown metric{suffix}", details={"suggestions": hint})

    def measure(self, name: str) -> SemanticMeasure:
        return self._measures.get(name)

    def dimension(self, name: str) -> SemanticDimension:
        return self._dimensions.get(name)

    def entity(self, name: str) -> SemanticEntity:
        return self._entities.get(name)

    def time_dimension(self, name: str | None) -> SemanticDimension:
        if name is not None:
            dim = self.dimension(name)
            if dim.type != "time":
                raise SchemaError("the requested time dimension is not a time dimension")
            return dim
        times = [d for d in self.model.dimensions if d.type == "time"]
        if len(times) != 1:
            names = ", ".join(d.name for d in times) or "none"
            raise SchemaError(f"name the time dimension explicitly (available: {names})")
        return times[0]
