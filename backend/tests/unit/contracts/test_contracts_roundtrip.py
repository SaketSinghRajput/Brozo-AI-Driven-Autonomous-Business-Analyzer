"""Every contract round-trips model -> JSON -> model; contracts are frozen; schemas are current."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import BaseModel, ValidationError

from app.data.domain import TRANSITIONS, DatasetState, can_transition
from app.evidence.domain import Evidence

from .contract_samples import SAMPLES

REPO = Path(__file__).resolve().parents[4]


def _export_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_schemas", REPO / "scripts/export_schemas.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("model", list(SAMPLES), ids=lambda m: m.__name__)
def test_round_trip(model: type[BaseModel]) -> None:
    original = model.model_validate(SAMPLES[model])
    again = model.model_validate_json(original.model_dump_json())
    assert again == original


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Evidence.model_validate({**SAMPLES[Evidence], "unexpected": 1})


def test_evidence_is_frozen() -> None:
    evidence = Evidence.model_validate(SAMPLES[Evidence])
    with pytest.raises(ValidationError, match="frozen"):
        evidence.operation = "changed"  # type: ignore[misc]  # frozen: mypy agrees


def test_every_exported_contract_has_a_sample() -> None:
    exported = set(_export_script().CONTRACTS.values())
    assert exported <= set(SAMPLES)


def test_exported_schemas_are_up_to_date() -> None:
    script = _export_script()
    outdated = script.stale()
    assert outdated == [], "run: uv run python ../scripts/export_schemas.py"


MINOR_1_1 = {"ChartSpec", "DashboardSpec"}  # 1.1 added optional fields; readers accept 1.0


def test_exported_schemas_carry_schema_version() -> None:
    for text in _export_script().render().values():
        schema = json.loads(text)
        version = schema["properties"]["schema_version"]
        if schema["title"] in MINOR_1_1:
            assert (version["enum"], version["default"]) == (["1.0", "1.1"], "1.1")
        else:
            assert version["const"] == "1.0", schema["title"]


def test_dashboard_spec_1_0_documents_are_still_read() -> None:
    from app.dashboard.domain import DashboardSpec

    old = {**SAMPLES[DashboardSpec], "schema_version": "1.0"}
    assert DashboardSpec.model_validate(old).schema_version == "1.0"


def test_dataset_state_machine_matches_design() -> None:
    assert can_transition(DatasetState.UPLOADED, DatasetState.VALIDATING)
    assert can_transition(DatasetState.READY, DatasetState.SEMANTICALLY_MAPPED)
    assert not can_transition(DatasetState.UPLOADED, DatasetState.READY)
    assert not TRANSITIONS[DatasetState.VALIDATION_FAILED]
    assert set(TRANSITIONS) == set(DatasetState)
