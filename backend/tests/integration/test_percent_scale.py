"""GAPS-1: a percent column is a fraction end to end (TRD 25.1, ADR-0021).

"12%" in the upload -> 0.12 stored -> profile stats in [0, 1] -> semantic unit `pct` -> a metric
query returns 0.12 with unit pct -> the validator accepts the text "12%" for that evidence.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.ids import UuidV7Ids
from app.validator.domain import ValidationStatus
from app.validator.service import ValidationService
from tests.unit.validator.factories import number, request
from tests.unit.validator.factories import text as seg

from .dataset_env import Env


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "discounts.csv"
    rows = ["order_id,order_date,region,revenue,discount_pct"]
    for i in range(40):
        rows.append(
            f"{1000 + i},2025-{1 + i % 12:02d}-{1 + i % 27:02d},"
            f"{('North', 'South')[i % 2]},{100 + i * 7},12%"
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_percent_column_is_a_fraction_end_to_end(env: Env, tmp_path: Path) -> None:
    r = env.upload(_csv(tmp_path))
    assert r.status_code == 201, r.text
    ids: dict[str, str] = r.json()
    env.settle()
    base = f"/{ids['dataset_id']}/versions/{ids['version_id']}"

    profile = env.get(f"{base}/profile").json()
    column = next(c for c in profile["columns"] if c["name"] == "discount_pct")
    assert column["physical_type"] == "float"
    assert column["unit_hint"] == "percent"
    assert (column["numeric"]["min"], column["numeric"]["max"]) == (0.12, 0.12)

    model: dict[str, Any] = env.get(f"{base}/semantic-model").json()
    measure = next(m for m in model["measures"] if m["column"] == "discount_pct")
    assert (measure["unit"], measure["default_agg"]) == ("pct", "avg"), measure

    body = {
        "dataset_version_id": ids["version_id"],
        "semantic_model_version": model["version"],
        "request": {"metric": measure["name"]},
    }
    q = env.client.post("/api/v1/analytics/query", json=body)
    assert q.status_code == 200, q.text
    result = q.json()
    assert (result["value"], result["unit"]) == (pytest.approx(0.12), "pct")
    evidence_id = result["evidence_id"]

    answer = request(
        seg("The average discount was 12%, shown as "),
        number(evidence_id, fmt="pct:0"),
        evidence_ids=[evidence_id],
    )
    validated = asyncio.run(
        ValidationService(env.container.uow, UuidV7Ids()).validate(env.scope, answer)
    )
    assert validated.status is ValidationStatus.VALID, validated.issues
