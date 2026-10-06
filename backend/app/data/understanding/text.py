"""Header tokenisation shared by typing, entity, relationship, domain and alias matching.

``"CustomerID"``, ``"customer_id"`` and ``"Customer ID"`` all become ``("customer", "id")``.
Common abbreviations are expanded (``amt`` -> ``amount``) so lexicons list each concept once.
"""

import re
from collections.abc import Iterable

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT = re.compile(r"[^0-9a-zA-Z]+")

SYNONYMS: dict[str, str] = {
    "amt": "amount",
    "qty": "quantity",
    "rev": "revenue",
    "cust": "customer",
    "dt": "date",
    "pct": "percent",
    "perc": "percent",
    "num": "number",
    "no": "number",
    "nbr": "number",
    "desc": "description",
    "prod": "product",
    "cat": "category",
    "addr": "address",
    "tel": "phone",
    "mob": "mobile",
    "val": "value",
    "yr": "year",
    "mth": "month",
    "mo": "month",
}


def tokens(name: str) -> tuple[str, ...]:
    spaced = _CAMEL.sub(" ", name.strip())
    raw = [t.lower() for t in _SPLIT.split(spaced) if t]
    return tuple(SYNONYMS.get(t, t) for t in raw)


def phrase(name: str) -> str:
    """Canonical form used for exact comparisons: tokens joined by one space."""
    return " ".join(tokens(name))


def singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def contains_phrase(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """True when ``needle`` occurs as a contiguous token run inside ``haystack``."""
    n = len(needle)
    if n == 0:
        return False
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def title(name_tokens: Iterable[str]) -> str:
    return " ".join(t.capitalize() for t in name_tokens)
