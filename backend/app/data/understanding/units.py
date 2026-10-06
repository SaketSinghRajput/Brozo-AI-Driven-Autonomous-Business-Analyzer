"""Unit detection (TASK-SEM-004; design 15 step 4).

Rules, first match wins:
- percentage -> ``pct``;
- currency amount / price -> ``currency:<ISO>`` from an ISO code in the header, else a currency
  symbol in the top values, else the workspace default currency, else plain ``currency``;
  a ``per <unit>`` suffix appends ``/<unit>`` (``market_price_per_t`` -> ``currency:INR/tonne``);
- rate with ``<num>_per_<den>`` -> ``<num>/<den>`` (``yield_t_per_ha`` -> ``t/ha``);
- quantity / count -> a physical unit token in the header (``area_ha`` -> ``hectare``) else
  ``count``;
- anything else -> ``None`` (unitless).
"""

from app.data.profiling.domain import ColumnProfile
from app.data.understanding.domain import SemanticType
from app.data.understanding.text import tokens

ISO_CODES = frozenset(
    {"inr", "usd", "eur", "gbp", "jpy", "cny", "aud", "cad", "sgd", "aed", "chf", "zar", "brl"}
)
SYMBOLS = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
PHYSICAL_UNITS = {
    "t": "tonne",
    "tonne": "tonne",
    "tonnes": "tonne",
    "ton": "tonne",
    "kg": "kg",
    "g": "gram",
    "ha": "hectare",
    "hectare": "hectare",
    "hectares": "hectare",
    "acre": "acre",
    "acres": "acre",
    "l": "litre",
    "litre": "litre",
    "litres": "litre",
    "km": "km",
    "m": "metre",
    "hours": "hour",
    "hrs": "hour",
    "minutes": "minute",
    "mins": "minute",
    "days": "day",
}
_CURRENCY = {SemanticType.CURRENCY_AMOUNT, SemanticType.PRICE}


def detect_unit(
    header: str,
    semantic_type: SemanticType,
    profile: ColumnProfile | None = None,
    default_currency: str | None = None,
) -> str | None:
    toks = tokens(header)
    before, per, after = _split_per(toks)
    if semantic_type is SemanticType.PERCENTAGE:
        return "pct"
    if semantic_type in _CURRENCY:
        code = _currency_code(toks, profile) or default_currency
        unit = f"currency:{code.upper()}" if code else "currency"
        if per and after:
            unit += "/" + PHYSICAL_UNITS.get(after[0], after[0])
        return unit
    if semantic_type is SemanticType.RATE_OR_RATIO:
        if per and before and after:
            return f"{before[-1]}/{after[0]}"
        return None
    if semantic_type in {SemanticType.QUANTITY, SemanticType.COUNT}:
        for tok in reversed(before):
            if tok in PHYSICAL_UNITS:
                return PHYSICAL_UNITS[tok]
        return "count"
    return None


def _split_per(toks: tuple[str, ...]) -> tuple[tuple[str, ...], bool, tuple[str, ...]]:
    if "per" not in toks:
        return toks, False, ()
    i = toks.index("per")
    return toks[:i], True, toks[i + 1 :]


def _currency_code(toks: tuple[str, ...], profile: ColumnProfile | None) -> str | None:
    for tok in toks:
        if tok in ISO_CODES:
            return tok.upper()
    if profile is not None and profile.categorical and not profile.categorical.masked:
        for value in profile.categorical.values:
            for symbol, code in SYMBOLS.items():
                if symbol in value.value:
                    return code
            for tok in tokens(value.value):
                if tok in ISO_CODES:
                    return tok.upper()
    return None
