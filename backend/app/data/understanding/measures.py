"""MetricDetector: measures + additivity (TASK-SCHEMA-008; TRD 11.3 "Measure", 11.4).

| semantic type   | kind                            | additivity                  |
|-----------------|---------------------------------|-----------------------------|
| currency_amount | amount (balance if snapshot)    | additive (semi_additive)    |
| quantity        | quantity (balance if snapshot)  | additive (semi_additive)    |
| count           | count                           | additive                    |
| price           | price / unit_cost               | non_additive                |
| rate_or_ratio   | rate / ratio                    | non_additive                |
| percentage      | percentage                      | non_additive                |

"Snapshot" = the header names a level measured at a point in time (MRR, balance, stock, ...):
summable across dimensions, not over time (TRD 11.4 semi-additive row).
"""

from collections.abc import Sequence
from typing import Literal

from app.data.profiling.domain import ProfileResult
from app.data.understanding.domain import Additivity, ColumnUnderstanding, Measure, SemanticType
from app.data.understanding.text import tokens
from app.data.understanding.units import detect_unit

T = SemanticType
MeasureKind = Literal[
    "amount", "quantity", "count", "price", "unit_cost", "rate", "ratio", "percentage", "balance"
]
SNAPSHOT_TOKENS = frozenset(
    {"mrr", "arr", "balance", "stock", "inventory", "headcount", "level", "outstanding",
     "backlog", "subscribers", "aum"}
)  # fmt: skip


class MetricDetector:
    def detect(
        self,
        profile: ProfileResult,
        columns: Sequence[ColumnUnderstanding],
        key_columns: frozenset[str] = frozenset(),
        default_currency: str | None = None,
    ) -> list[Measure]:
        by_name = {c.name: c for c in profile.columns}
        measures = []
        for col in columns:
            if col.name in key_columns or col.physical_type not in {"int", "float", "decimal"}:
                continue
            classified = classify_measure(col.name, col.semantic_type)
            if classified is None:
                continue
            kind, additivity = classified
            unit = detect_unit(col.name, col.semantic_type, by_name[col.name], default_currency)
            measures.append(Measure(column=col.name, kind=kind, additivity=additivity, unit=unit))
        return measures


def classify_measure(
    header: str, semantic_type: SemanticType
) -> tuple[MeasureKind, Additivity] | None:
    toks = set(tokens(header))
    snapshot = bool(toks & SNAPSHOT_TOKENS)
    if semantic_type is T.CURRENCY_AMOUNT:
        return ("balance", "semi_additive") if snapshot else ("amount", "additive")
    if semantic_type is T.QUANTITY:
        return ("balance", "semi_additive") if snapshot else ("quantity", "additive")
    if semantic_type is T.COUNT:
        return ("count", "additive")
    if semantic_type is T.PRICE:
        return ("unit_cost" if "cost" in toks else "price", "non_additive")
    if semantic_type is T.RATE_OR_RATIO:
        return ("ratio" if "ratio" in toks else "rate", "non_additive")
    if semantic_type is T.PERCENTAGE:
        return ("percentage", "non_additive")
    return None
