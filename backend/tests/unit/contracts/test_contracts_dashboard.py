"""The example dashboard specs the frontend builds against (TASK-DASH-001) validate."""

import pytest

from app.dashboard.domain import DashboardSpec, UnavailableWidget

from .contract_samples import EXAMPLES

NAMES = sorted(p.stem for p in EXAMPLES.glob("dashboard_*.json"))


def _load(name: str) -> DashboardSpec:
    return DashboardSpec.model_validate_json(
        (EXAMPLES / f"{name}.json").read_text(encoding="utf-8")
    )


def test_all_four_examples_exist() -> None:
    expected = ["agriculture", "retail", "saas", "unavailable"]
    assert [f"dashboard_{n}" for n in expected] == NAMES


@pytest.mark.parametrize("name", NAMES)
def test_example_validates_and_round_trips(name: str) -> None:
    spec = _load(name)
    assert DashboardSpec.model_validate_json(spec.model_dump_json()) == spec


def test_unavailable_example_has_only_reasoned_unavailable_widgets() -> None:
    spec = _load("dashboard_unavailable")
    assert spec.widgets
    assert all(isinstance(w, UnavailableWidget) and w.reason_code for w in spec.widgets)
