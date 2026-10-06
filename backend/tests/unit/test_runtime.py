from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, SystemClock
from app.core.config import AppConfig
from app.core.container import build_container
from app.core.ids import SequentialIds, UuidV7Ids


def test_system_clock_is_utc() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_frozen_clock_advances() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(timedelta(hours=1))
    assert clock.now() == datetime(2026, 1, 1, 1, tzinfo=UTC)


def test_frozen_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 1, 1))


def test_uuid7_ids_are_prefixed_unique_and_time_ordered() -> None:
    ids = UuidV7Ids()
    generated = [ids.new("ev") for _ in range(200)]
    assert all(i.startswith("ev_") for i in generated)
    assert len(set(generated)) == 200
    assert generated == sorted(generated)


@pytest.mark.parametrize("prefix", ["", "EV", "dsv-1", "toolongprefix", "../x"])
def test_invalid_prefix_rejected(prefix: str) -> None:
    with pytest.raises(ValueError, match="invalid id prefix"):
        UuidV7Ids().new(prefix)


def test_sequential_ids_are_deterministic() -> None:
    ids = SequentialIds()
    assert [ids.new("ds"), ids.new("ds")] == ["ds_000001", "ds_000002"]


def test_container_defaults_and_overrides(config: AppConfig) -> None:
    default = build_container(config)
    assert isinstance(default.clock, SystemClock)
    fake_clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    custom = build_container(config, clock=fake_clock, ids=SequentialIds())
    assert custom.clock is fake_clock
    assert custom.ids.new("x") == "x_000001"
