"""TASK-ANALYSIS-008…013: every service against hand-computed numbers (worked in comments)."""

from datetime import date

import pytest

from app.analytics.domain import MetricRequest
from app.analytics.services.aggregation import aggregate
from app.analytics.services.contribution import (
    contribution_to_change,
    price_volume_mix,
    ratio_decomposition,
    share_of_total,
)
from app.analytics.services.growth import cagr, growth, pct_change, period_growth
from app.analytics.services.ranking import RankMethod, rank
from app.analytics.services.statistics import compare_groups, correlate, describe
from app.analytics.services.timeseries import fill_calendar, resample, trend_test
from app.analytics.sql.runner import MetricQueryRunner
from app.core.errors import SchemaError

# -- growth -------------------------------------------------------------------------------------


def test_pct_change_and_units() -> None:
    # TRD 25.1: pct and pp are fractions (0.25 = 25 %, 0.03 = 3 percentage points)
    assert pct_change(200, 250).value == 0.25  # (250 - 200) / 200
    assert pct_change(-200, -100).value == 0.5  # divides by |x0|
    pp = growth(0.12, 0.15, "pct")
    assert pp.unit == "pp"
    assert pp.value == pytest.approx(0.03)
    assert growth(12.0, 15.0, "currency:INR").unit == "pct"


def test_zero_base_is_undefined_not_inf_or_zero() -> None:
    result = pct_change(0, 5)
    assert (result.value, result.undefined) == (None, "BASE_IS_ZERO")
    assert pct_change(None, 5).undefined == "MISSING_VALUE"


def test_period_growth() -> None:
    series = [("Jan", 100.0), ("Feb", 110.0), ("Mar", None), ("Apr", 121.0)]
    out = period_growth(series, "count")
    assert [p.period for p in out] == ["Feb", "Mar", "Apr"]
    assert out[0].growth.value == pytest.approx(0.10)
    assert [p.growth.undefined for p in out[1:]] == ["MISSING_VALUE", "MISSING_VALUE"]


def test_cagr() -> None:
    # 731 days = 2.0014 years; 1.21 ** (1 / 2.0014) - 1 = 0.09993 (9.993 %)
    two_years = cagr(100, 121, date(2024, 1, 1), date(2026, 1, 1))
    assert two_years.value == pytest.approx(0.09993, abs=1e-5)
    assert cagr(0, 5, date(2024, 1, 1), date(2025, 1, 1)).undefined == "NON_POSITIVE_START"
    assert cagr(5, -1, date(2024, 1, 1), date(2025, 1, 1)).undefined == "NEGATIVE_END"
    assert cagr(5, 6, date(2024, 1, 1), date(2024, 1, 1)).undefined == "ZERO_DURATION"


# -- ranking ------------------------------------------------------------------------------------

ITEMS: list[tuple[str, float | None]] = [
    ("a", 10.0),
    ("b", 20.0),
    ("c", 20.0),
    ("d", 5.0),
    ("e", None),
]


def _ranks(method: RankMethod, descending: bool = True) -> list[tuple[str, int, bool]]:
    result = rank(ITEMS, method=method, descending=descending)
    return [(i.key, i.rank, i.tied) for i in result.items]


def test_rank_methods_make_ties_explicit() -> None:
    assert _ranks("competition") == [
        ("b", 1, True),
        ("c", 1, True),
        ("a", 3, False),
        ("d", 4, False),
    ]
    assert _ranks("dense") == [("b", 1, True), ("c", 1, True), ("a", 2, False), ("d", 3, False)]
    assert _ranks("row") == [("b", 1, True), ("c", 2, True), ("a", 3, False), ("d", 4, False)]
    assert _ranks("competition", descending=False)[0] == ("d", 1, False)
    assert rank(ITEMS).excluded_nulls == 1


def test_top_n_with_ties() -> None:
    top1 = rank(ITEMS, top_n=1)
    assert ([i.key for i in top1.items], top1.ties_at_cutoff) == (["b", "c"], True)
    row1 = rank(ITEMS, method="row", top_n=1)
    assert ([i.key for i in row1.items], row1.ties_at_cutoff) == (["b"], True)
    top3 = rank(ITEMS, top_n=3)
    assert ([i.key for i in top3.items], top3.ties_at_cutoff) == (["b", "c", "a"], False)


# -- statistics ---------------------------------------------------------------------------------


def test_describe() -> None:
    d = describe([1, 2, 3, 4, None])
    assert (d.n, d.missing, d.mean, d.min, d.max) == (4, 1, 2.5, 1, 4)
    assert d.std == pytest.approx((5 / 3) ** 0.5)  # sample std: sqrt(5 / 3)
    assert (d.q1, d.median, d.q3) == (1.75, 2.5, 3.25)
    assert describe([]).mean is None
    assert describe([7]).std is None


def test_correlate_reports_n_and_caveats() -> None:
    c = correlate([1, 2, 3, 4, None], [2, 4, 6, 8, 100])
    assert c.r == pytest.approx(1.0)
    assert c.n == 4
    assert any("not evidence of causation" in x for x in c.caveats)
    assert any("small sample (n=4)" in x for x in c.caveats)
    assert correlate([1, 2, 3], [3, 2, 1]).r == pytest.approx(-1.0)
    assert correlate([1, 2, 3, 4], [1, 4, 9, 16], "spearman").r == pytest.approx(1.0)
    assert correlate([1, 2], [1, 2]).undefined == "TOO_FEW_PAIRS"
    assert correlate([1, 1, 1], [1, 2, 3]).undefined == "CONSTANT_INPUT"


def test_compare_groups_is_descriptive() -> None:
    result = compare_groups({"A": [1, 3], "B": [5]})
    assert result.overall_mean == 3.0
    assert result.difference_from_overall == {"A": -1.0, "B": 2.0}
    assert "no significance test" in result.caveats[0]


# -- contribution -------------------------------------------------------------------------------


def test_share_of_total() -> None:
    result = share_of_total({"a": 30, "b": 70})
    assert [(i.key, i.share_pct) for i in result.items] == [("b", 0.7), ("a", 0.3)]
    assert share_of_total({"a": 0}).undefined == "TOTAL_IS_ZERO"
    assert "negative" in share_of_total({"a": -10, "b": 30}).caveats[0]


def test_contribution_to_change() -> None:
    # North 245 → 200 (Δ -45), South 100 → 340 (Δ +240); total Δ = 195
    result = contribution_to_change({"North": 245, "South": 100}, {"North": 200, "South": 340})
    assert result.total_delta == 195
    assert [(i.key, i.delta) for i in result.items] == [("South", 240), ("North", -45)]
    assert result.items[0].share_pct == pytest.approx(1.230769, abs=1e-6)
    assert result.items[1].share_pct == pytest.approx(-0.230769, abs=1e-6)
    new_member = contribution_to_change({"A": 10}, {"A": 10, "B": 5})
    assert new_member.items[0].key == "B"
    assert new_member.caveats
    assert contribution_to_change({"A": 1}, {"A": 1}).undefined == "NO_TOTAL_CHANGE"


def test_price_volume_mix() -> None:
    # A: 10 @ 10 → 12 @ 11; B: 5 @ 20 → 3 @ 20; C new (50). Q0 = Q1 = 15, P0bar = 200 / 15.
    # volume = 0; mix = (12*10 + 3*20) - 15 * 200/15 = -20; price = 12 * 1 = 12; new = 50.
    pvm = price_volume_mix(
        {"A": (100, 10), "B": (100, 5)}, {"A": (132, 12), "B": (60, 3), "C": (50, 5)}
    )
    assert pvm.volume_effect == pytest.approx(0)
    assert pvm.mix_effect == pytest.approx(-20)
    assert pvm.price_effect == pytest.approx(12)
    assert (pvm.new_items_effect, pvm.discontinued_items_effect) == (50, 0)
    assert pvm.total_change == 42
    assert pvm.residual == pytest.approx(0, abs=1e-9)


def test_ratio_decomposition() -> None:
    # AOV: North 245/2 → 200/2, South 100/1 → 340/2. R 115 → 135 (Δ 20).
    # rate = .5*(100-122.5) + .5*(170-100) = 23.75; mix = (.5-2/3)*122.5 + (.5-1/3)*100 = -3.75
    r = ratio_decomposition({"N": (245, 2), "S": (100, 1)}, {"N": (200, 2), "S": (340, 2)})
    assert (r.ratio_before, r.ratio_after, r.change) == (115, 135, 20)
    assert r.rate_effect == pytest.approx(23.75)
    assert r.mix_effect == pytest.approx(-3.75)
    assert r.entry_exit_effect == pytest.approx(0)
    assert r.residual == pytest.approx(0, abs=1e-9)


# -- aggregation --------------------------------------------------------------------------------


def test_aggregate_rolls_up_with_additivity_checks(runner: MetricQueryRunner) -> None:
    table = runner.run(MetricRequest(metric="Revenue", dimensions=["Region", "Category"])).table
    rolled = aggregate(table, "value", "sum", ["Region"], "additive")
    assert [(g.key, g.value) for g in rolled.groups] == [(["North"], 445.0), (["South"], 440.0)]
    assert (rolled.total, rolled.unit) == (885.0, "currency:INR")
    assert aggregate(table, "value", "count", ["Region"], "non_additive").total == 4
    with pytest.raises(SchemaError, match="non-additive"):
        aggregate(table, "value", "sum", ["Region"], "non_additive")
    monthly = runner.run(
        MetricRequest.model_validate({"metric": "Quantity", "time": {"grain": "month"}})
    ).table
    with pytest.raises(SchemaError, match="across time"):
        aggregate(monthly, "value", "sum", [], "semi_additive")
    assert aggregate(monthly, "value", "sum", ["period"], "semi_additive").total == 11


# -- time series --------------------------------------------------------------------------------


def test_resample() -> None:
    points: list[tuple[date, float | None]] = [
        (date(2026, 1, 5), 1.0),
        (date(2026, 1, 20), 2.0),
        (date(2026, 2, 1), None),
        (date(2026, 3, 3), 4.0),
    ]
    assert resample(points, "month") == [
        (date(2026, 1, 1), 3.0),
        (date(2026, 2, 1), None),
        (date(2026, 3, 1), 4.0),
    ]
    assert resample(points, "month", "mean")[0] == (date(2026, 1, 1), 1.5)


def test_fill_calendar_reports_missing_periods() -> None:
    points: list[tuple[date, float | None]] = [(date(2026, 1, 1), 10.0), (date(2026, 3, 1), 30.0)]
    start, end = date(2026, 1, 1), date(2026, 4, 30)
    none = fill_calendar(points, "month", start, end)
    assert none.missing_periods == [date(2026, 2, 1), date(2026, 4, 1)]
    assert [p.value for p in none.points] == [10.0, None, 30.0, None]
    zero = fill_calendar(points, "month", start, end, "zero")
    assert (zero.points[1].value, zero.points[1].filled) == (0.0, True)
    forward = fill_calendar(points, "month", start, end, "forward")
    assert [p.value for p in forward.points] == [10.0, 10.0, 30.0, 30.0]
    assert forward.missing_periods == none.missing_periods


def test_trend_test() -> None:
    # 1..5: S = 10, Var = 5*4*15/18 = 16.667, z = 9/4.082 = 2.2045, p = 0.0275
    up = trend_test([1, 2, 3, 4, 5])
    assert (up.s, up.tau, up.sen_slope, up.direction) == (10, 1.0, 1.0, "increasing")
    assert up.z == pytest.approx(2.2045, abs=1e-4)
    assert up.p_value == pytest.approx(0.0275, abs=1e-4)
    assert trend_test([5, 4, 3, 2, 1]).direction == "decreasing"
    assert trend_test([1, 3, 2, 4, 1, 3]).direction == "no_trend"
    assert trend_test([2, 2, 2, 2]).undefined == "ALL_TIED"
    assert trend_test([1, 2, None]).undefined == "TOO_FEW_POINTS"
