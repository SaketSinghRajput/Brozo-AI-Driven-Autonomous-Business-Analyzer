"""Display formats, number extraction, tolerance and direction words (TASK-VAL-003)."""

import pytest

from app.validator.numbers import (
    Candidate,
    DisplayFormat,
    Unit,
    extract_numbers,
    is_coarse,
    parse_format,
    rounds_to,
    unit_of,
)
from app.validator.text import direction_at, sentences


@pytest.mark.parametrize(
    ("raw", "unit"),
    [
        ("pct", Unit("pct")),
        ("currency:INR", Unit("currency", "INR")),
        ("currency:inr", Unit("plain")),
        ("kg", Unit("plain")),
        (None, Unit("plain")),
    ],
)
def test_unit_of(raw: str | None, unit: Unit) -> None:
    assert unit_of(raw) == unit


@pytest.mark.parametrize(
    ("spec", "fmt"),
    [
        ("pct:1", DisplayFormat(Unit("pct"), 1)),
        ("pp", DisplayFormat(Unit("pp"), 0)),
        ("number:2", DisplayFormat(Unit("plain"), 2)),
        ("count", DisplayFormat(Unit("count"), 0)),
        ("currency:INR:2", DisplayFormat(Unit("currency", "INR"), 2)),
        ("currency:USD", DisplayFormat(Unit("currency", "USD"), 0)),
        ("pct:12", None),
        ("percent", None),
    ],
)
def test_parse_format(spec: str, fmt: DisplayFormat | None) -> None:
    assert parse_format(spec) == fmt


def test_extraction_units_scales_and_skips() -> None:
    found = extract_numbers(
        "Q3 FY2026: in 2026 sales were ₹12.5 lakh, EUR 3k, \u22122 pp, 7 percentage points, "
        "3rd place, 10:15, 12/08/2026, 2000 units"
    )
    shown = [(n.value, n.decimals, n.scale, n.unit.kind, n.signed) for n in found]
    assert shown == [
        (12.5, 1, 1e5, "currency", False),
        (3.0, 0, 1e3, "currency", False),
        (-2.0, 0, 1.0, "pp", True),
        (7.0, 0, 1.0, "pp", False),
        (2000.0, 0, 1.0, "plain", False),
    ]
    assert found[1].unit.currency == "EUR"


def test_tolerance_is_half_a_displayed_digit() -> None:
    (n,) = extract_numbers("14.2%")
    assert rounds_to(n, Candidate(-0.14249, Unit("pct"), "e"))
    assert not rounds_to(n, Candidate(-0.14251, Unit("pct"), "e"))
    (zero,) = extract_numbers("0%")
    tiny = Candidate(0.004, Unit("pct"), "e")
    assert rounds_to(zero, tiny)
    assert is_coarse(zero, tiny)
    assert not is_coarse(zero, Candidate(0.0, Unit("pct"), "e"))


@pytest.mark.parametrize(
    ("sentence", "direction"),
    [
        ("Revenue fell 5%", "down"),
        ("Revenue rose by about 5%", "up"),
        ("Revenue fell to 5%", None),
        ("It was 5% higher", "up"),
        ("It was 5% lower", "down"),
        ("Orders rose. Revenue was 5%", None),
    ],
)
def test_direction_words(sentence: str, direction: str | None) -> None:
    start = sentence.index("5")
    assert direction_at(sentence, start, start + 2) == direction


def test_sentences_split_on_terminators_not_decimals() -> None:
    assert sentences("Up 14.2%. Down 3%!\nFlat") == ["Up 14.2%", " Down 3%", "Flat"]
