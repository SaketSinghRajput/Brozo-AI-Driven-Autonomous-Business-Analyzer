"""Shared scoring + abstention helper (TASK-SCHEMA-002)."""

import pytest

from app.data.understanding.config import Abstention, default_config, load_config
from app.data.understanding.scoring import decide, rank, weighted_score

T = Abstention(tau=0.5, delta=0.1)


def test_weighted_score_normalises_by_weight_sum_and_clips() -> None:
    assert weighted_score({"a": 1.0, "b": 0.0}, {"a": 3, "b": 1}) == pytest.approx(0.75)
    assert weighted_score({"a": 7.0}, {"a": 1, "b": 1}) == pytest.approx(0.5)


def test_weighted_score_rejects_unconfigured_signal() -> None:
    with pytest.raises(ValueError, match="without a configured weight"):
        weighted_score({"mystery": 1.0}, {"a": 1})


def test_decide_picks_clear_winner() -> None:
    d = decide({"price": 0.8, "amount": 0.5}, T)
    assert (d.label, d.abstained, d.runner_up) == ("price", False, "amount")
    assert d.confidence == 0.8


def test_decide_abstains_below_tau() -> None:
    d = decide({"price": 0.45, "amount": 0.1}, T)
    assert (d.label, d.top, d.reason) == ("unknown", "price", "below_tau")


def test_decide_abstains_on_small_margin() -> None:
    d = decide({"price": 0.8, "amount": 0.75}, T)
    assert (d.label, d.reason) == ("unknown", "margin_below_delta")


def test_decide_without_candidates() -> None:
    assert decide({}, T).reason == "no_candidates"


def test_rank_is_deterministic_on_ties() -> None:
    assert rank({"b": 0.5, "a": 0.5, "c": 0.9}) == [("c", 0.9), ("a", 0.5), ("b", 0.5)]


def test_shipped_config_is_versioned_and_valid() -> None:
    cfg = default_config()
    assert cfg.version.startswith("understanding-config@")
    assert load_config() == cfg
