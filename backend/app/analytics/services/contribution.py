"""ContributionService (TASK-ANALYSIS-012, design §22 + §43, TRD §28).

Formulas (all exact; the reported ``residual`` is the floating-point remainder, about 0):

share_of_total: share_i = x_i / sum(x) (a fraction, TRD §25.1 ``pct``). sum(x) = 0 → undefined.
Negative members → caveat.

contribution_to_change (additive metric M = sum of members): d_i = M_i(t1) - M_i(t0), a member
absent in one period counts as 0 there; share_i = d_i / sum(d) (fraction). sum(d) = 0 → undefined.

price_volume_mix (Revenue = sum p_i q_i with p_i = revenue_i / q_i), over items C present with
volume > 0 in both periods; Q = sum_C q, P0bar = sum_C q0 p0 / Q0:
    volume = (Q1 - Q0) * P0bar
    mix    = sum_C q1 p0 - Q1 * P0bar
    price  = sum_C q1 (p1 - p0)
    new    = revenue of items only in t1;  discontinued = -revenue of items only in t0
    volume + mix + price + new + discontinued = R1 - R0

ratio_decomposition (R = sum N / sum D, e.g. AOV = revenue / orders), over segments S with D > 0
in both periods; w = D_s / D_S, r = N_s / D_s:
    rate = sum_S w1 (r1 - r0);  mix = sum_S (w1 - w0) r0
    entry_exit = (R1 - R0) - (R1_S - R0_S)   (segments appearing or disappearing)
    rate + mix + entry_exit = R1 - R0
"""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShareItem(Contract):
    key: str
    value: float
    share_pct: float | None


class ShareResult(Contract):
    total: float
    items: list[ShareItem]
    caveats: list[str]
    undefined: Literal["TOTAL_IS_ZERO"] | None = None


class ChangeItem(Contract):
    key: str
    before: float
    after: float
    delta: float
    share_pct: float | None  # share of the total change


class ChangeResult(Contract):
    total_before: float
    total_after: float
    total_delta: float
    items: list[ChangeItem]  # ordered by |delta| descending, then key
    caveats: list[str]
    undefined: Literal["NO_TOTAL_CHANGE"] | None = None


class PriceVolumeMix(Contract):
    volume_effect: float
    mix_effect: float
    price_effect: float
    new_items_effect: float
    discontinued_items_effect: float
    total_change: float
    residual: float
    formula: str = "volume=(Q1-Q0)*P0bar; mix=sum(q1*p0)-Q1*P0bar; price=sum(q1*(p1-p0))"


class RatioDecomposition(Contract):
    ratio_before: float | None
    ratio_after: float | None
    change: float | None
    rate_effect: float
    mix_effect: float
    entry_exit_effect: float | None
    residual: float | None


def _clean(values: Mapping[str, float | None]) -> dict[str, float]:
    return {k: float(v) for k, v in values.items() if v is not None}


def share_of_total(values: Mapping[str, float | None]) -> ShareResult:
    xs = _clean(values)
    total = sum(xs.values())
    caveats = ["null members were excluded"] if len(xs) != len(values) else []
    if any(v < 0 for v in xs.values()):
        caveats.append("some members are negative; shares can exceed 100% or be negative")
    undefined: Literal["TOTAL_IS_ZERO"] | None = "TOTAL_IS_ZERO" if total == 0 else None
    items = [
        ShareItem(key=k, value=v, share_pct=None if undefined else v / total)
        for k, v in sorted(xs.items(), key=lambda kv: (-abs(kv[1]), kv[0]))
    ]
    return ShareResult(total=total, items=items, caveats=caveats, undefined=undefined)


def contribution_to_change(
    before: Mapping[str, float | None], after: Mapping[str, float | None]
) -> ChangeResult:
    b, a = _clean(before), _clean(after)
    keys = sorted(set(b) | set(a))
    deltas = {k: a.get(k, 0.0) - b.get(k, 0.0) for k in keys}
    total_delta = sum(deltas.values())
    caveats = []
    if set(b) != set(a):
        caveats.append("members missing in one period are counted as 0 in that period")
    undefined: Literal["NO_TOTAL_CHANGE"] | None = "NO_TOTAL_CHANGE" if total_delta == 0 else None
    items = [
        ChangeItem(
            key=k,
            before=b.get(k, 0.0),
            after=a.get(k, 0.0),
            delta=deltas[k],
            share_pct=None if undefined else deltas[k] / total_delta,
        )
        for k in sorted(keys, key=lambda k: (-abs(deltas[k]), k))
    ]
    return ChangeResult(
        total_before=sum(b.values()),
        total_after=sum(a.values()),
        total_delta=total_delta,
        items=items,
        caveats=caveats,
        undefined=undefined,
    )


def _common(
    before: Mapping[str, tuple[float, float]], after: Mapping[str, tuple[float, float]]
) -> list[str]:
    return sorted(k for k in set(before) & set(after) if before[k][1] > 0 and after[k][1] > 0)


def price_volume_mix(
    before: Mapping[str, tuple[float, float]], after: Mapping[str, tuple[float, float]]
) -> PriceVolumeMix:
    """Inputs map item → (revenue, volume)."""
    common = _common(before, after)
    q0 = sum(before[k][1] for k in common)
    q1 = sum(after[k][1] for k in common)
    p0 = {k: before[k][0] / before[k][1] for k in common}
    p1 = {k: after[k][0] / after[k][1] for k in common}
    p0_bar = sum(before[k][0] for k in common) / q0 if q0 else 0.0
    volume = (q1 - q0) * p0_bar
    mix = sum(after[k][1] * p0[k] for k in common) - q1 * p0_bar
    price = sum(after[k][1] * (p1[k] - p0[k]) for k in common)
    new = sum(v[0] for k, v in after.items() if k not in common)
    gone = -sum(v[0] for k, v in before.items() if k not in common)
    total = sum(v[0] for v in after.values()) - sum(v[0] for v in before.values())
    return PriceVolumeMix(
        volume_effect=volume,
        mix_effect=mix,
        price_effect=price,
        new_items_effect=new,
        discontinued_items_effect=gone,
        total_change=total,
        residual=total - (volume + mix + price + new + gone),
    )


def _ratio(parts: Mapping[str, tuple[float, float]]) -> float | None:
    den = sum(d for _, d in parts.values())
    return sum(n for n, _ in parts.values()) / den if den else None


def ratio_decomposition(
    before: Mapping[str, tuple[float, float]], after: Mapping[str, tuple[float, float]]
) -> RatioDecomposition:
    """Inputs map segment → (numerator, denominator)."""
    common = _common(before, after)
    d0 = sum(before[k][1] for k in common)
    d1 = sum(after[k][1] for k in common)
    rate = mix = 0.0
    for k in common:
        (n0, den0), (n1, den1) = before[k], after[k]
        w0, w1, r0, r1 = den0 / d0, den1 / d1, n0 / den0, n1 / den1
        rate += w1 * (r1 - r0)
        mix += (w1 - w0) * r0
    r_before, r_after = _ratio(before), _ratio(after)
    change = r_after - r_before if r_before is not None and r_after is not None else None
    entry_exit = residual = None
    if change is not None:
        c0 = _ratio({k: before[k] for k in common})
        c1 = _ratio({k: after[k] for k in common})
        common_change = c1 - c0 if c0 is not None and c1 is not None else 0.0
        entry_exit = change - common_change
        residual = change - (rate + mix + entry_exit)
    return RatioDecomposition(
        ratio_before=r_before,
        ratio_after=r_after,
        change=change,
        rate_effect=rate,
        mix_effect=mix,
        entry_exit_effect=entry_exit,
        residual=residual,
    )
