"""Calibration helpers for the data-understanding harness (ADR-0026, RES-017).

- ``ece``: expected calibration error, equal-width bins over confidence.
- ``sweep``: grid over (tau, delta); for each point, coverage = share of decisions not abstained,
  precision = share correct among those. Best = highest coverage whose precision >= floor
  (ties: higher precision, then the more conservative higher tau / delta).
Isotonic calibration is NOT applied: scikit-learn is not a backend dependency (ADR-0026 fallback).
"""

from collections.abc import Sequence
from typing import Any

TAUS = [round(0.30 + 0.025 * i, 3) for i in range(17)]  # 0.30 .. 0.70
DELTAS = [round(0.01 * i, 2) for i in range(21)]  # 0.00 .. 0.20


def ece(pairs: Sequence[tuple[float, bool]], bins: int = 10) -> float:
    if not pairs:
        return 0.0
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        members = [(c, ok) for c, ok in pairs if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if members:
            confidence = sum(c for c, _ in members) / len(members)
            accuracy = sum(1 for _, ok in members if ok) / len(members)
            total += len(members) / len(pairs) * abs(confidence - accuracy)
    return round(total, 4)


def sweep(
    decisions: Sequence[tuple[float, float, bool]], floor: float, current: tuple[float, float]
) -> dict[str, Any]:
    """``decisions`` = (top score, margin to runner-up, top label correct)."""
    points = []
    for tau, delta in [current] + [(t, d) for t in TAUS for d in DELTAS]:
        covered = [ok for score, margin, ok in decisions if score >= tau and margin >= delta]
        coverage = len(covered) / len(decisions) if decisions else 0.0
        precision = sum(covered) / len(covered) if covered else 1.0
        points.append(
            {"tau": tau, "delta": delta, "coverage": round(coverage, 4),
             "precision": round(precision, 4)}
        )  # fmt: skip
    here, points = points[0], points[1:]
    feasible = [p for p in points if p["precision"] >= floor]
    best = max(
        feasible,
        key=lambda p: (p["coverage"], p["precision"], p["tau"], p["delta"]),
        default=None,
    )
    return {
        "grid_points": len(points),
        "feasible_points": len(feasible),
        "best": best,
        "current": here,
    }
