"""Shared weighted scoring + abstention (TASK-SCHEMA-002; design 13 "Scoring (shared helper)").

``score = sum(w_k * signal_k) / sum(w_k)`` keeps every score in [0, 1] whatever the weights.
``decide`` abstains (returns the ``unknown`` label) when the top score is below ``tau`` or the
margin to the runner-up is below ``delta``. Ties are broken by label name so results never depend
on dict ordering.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app.data.understanding.config import Abstention

UNKNOWN = "unknown"


@dataclass(frozen=True)
class Decision:
    label: str  # the winning label, or ``unknown`` when abstaining
    top: str | None  # best label even when abstaining (for review flags / tie-breakers)
    runner_up: str | None
    score: float
    margin: float
    abstained: bool
    reason: str | None  # "below_tau" | "margin_below_delta" | "no_candidates"

    @property
    def confidence(self) -> float:
        """Top score, rounded; the calibration map (design 13) is the identity until RES-017."""
        return round(self.score, 4)


def weighted_score(signals: Mapping[str, float], weights: Mapping[str, float]) -> float:
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    unknown = set(signals) - set(weights)
    if unknown:
        raise ValueError(f"signals without a configured weight: {sorted(unknown)}")
    raw = sum(weights[name] * _clip(value) for name, value in signals.items())
    return raw / total


def rank(scores: Mapping[str, float]) -> list[tuple[str, float]]:
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def decide(scores: Mapping[str, float], thresholds: Abstention, unknown: str = UNKNOWN) -> Decision:
    ranked = rank(scores)
    if not ranked:
        return Decision(unknown, None, None, 0.0, 0.0, True, "no_candidates")
    top, top_score = ranked[0]
    runner_up, second = ranked[1] if len(ranked) > 1 else (None, 0.0)
    margin = top_score - second
    reason = None
    if top_score < thresholds.tau:
        reason = "below_tau"
    elif margin < thresholds.delta:
        reason = "margin_below_delta"
    label = unknown if reason else top
    return Decision(label, top, runner_up, top_score, margin, reason is not None, reason)


def _clip(value: float) -> float:
    return min(1.0, max(0.0, value))
