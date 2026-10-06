"""SchemaAnalyzer orchestration + idempotent UnderstandingService (TASK-SCHEMA-010)."""

import pytest

from app.core.scope import Scope, WorkspaceScope
from app.data.understanding.analyzer import SchemaAnalyzer, TableInput, UnderstandingService
from app.data.understanding.domain import Understanding
from tests.unit.data.understanding.factories import col, key, orders_profile, profile

SCOPE = WorkspaceScope(workspace_id="ws_abc123", principal_id="usr_1")


class FakeRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Understanding] = {}
        self.saves = 0

    def get(
        self, scope: Scope, dataset_version_id: str, engine_version: str
    ) -> Understanding | None:
        return self.rows.get((dataset_version_id, engine_version))

    def save(self, scope: Scope, understanding: Understanding) -> None:
        self.saves += 1
        self.rows[(understanding.dataset_version_id, understanding.engine_version)] = understanding


def test_analyzer_builds_consistent_understanding() -> None:
    analyzer = SchemaAnalyzer()
    result = analyzer.analyze([TableInput("orders", orders_profile())], default_currency="INR")
    u = result["orders"].understanding
    assert u.engine_version == analyzer.engine_version
    assert "understanding-config@" in u.engine_version
    assert [t.column for t in u.time_dimensions] == ["order_date"]
    assert {m.column for m in u.measures} == {"quantity", "unit_price"}
    assert u.entities[0].key_columns == ["order_id"]
    assert result["orders"].type_decisions["order_id"].label == "identifier"


def test_review_flags_on_low_confidence_and_missing_key() -> None:
    p = profile(
        col("x", "float", unique=0.9, card=900), col("amount", "float", unique=0.9, card=900)
    )
    u = SchemaAnalyzer().analyze([TableInput("t", p)])["t"].understanding
    assert "TYPE_UNCERTAIN:x" in u.review_flags
    assert "NO_PRIMARY_KEY" in u.review_flags
    assert "CURRENCY_UNKNOWN:amount" in u.review_flags


def test_analysis_is_deterministic() -> None:
    tables = [TableInput("orders", orders_profile())]
    assert SchemaAnalyzer().analyze(tables) == SchemaAnalyzer().analyze(tables)


def test_duplicate_table_names_rejected() -> None:
    p = profile(key("id"))
    with pytest.raises(ValueError, match="unique"):
        SchemaAnalyzer().analyze([TableInput("t", p), TableInput("t", p)])


def test_service_is_idempotent() -> None:
    repo = FakeRepository()
    service = UnderstandingService(SchemaAnalyzer(), repo)
    table = TableInput("orders", orders_profile())
    first = service.understand(SCOPE, table)
    assert service.understand(SCOPE, table) == first
    assert repo.saves == 1
