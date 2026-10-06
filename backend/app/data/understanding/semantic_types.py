"""SemanticTypeClassifier (TASK-SCHEMA-003/004; design 13, TRD 11.2).

``score(type) = w_name*name + w_value*value_pattern + w_stat*stat_fit + w_ctx*context`` per column,
then the shared ``decide`` abstains to ``unknown`` below tau / delta. Name matching is
most-specific-first: a multi-word lexicon phrase consumes its tokens, so ``unit_cost`` is a price
and not also a currency amount. Tokens after ``per`` are denominators (``yield_t_per_ha``) and do
not vote; a ``per`` header is a rate unless it names a price.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.data.profiling.domain import ColumnProfile, ProfileResult
from app.data.understanding.config import CONFIG_DIR, UnderstandingConfig, default_config
from app.data.understanding.domain import ColumnUnderstanding, SemanticType
from app.data.understanding.scoring import Decision, decide, weighted_score
from app.data.understanding.text import contains_phrase, tokens

T = SemanticType
LEXICON_PATH = CONFIG_DIR / "semantic_types.v1.json"
NUMERIC = frozenset({"int", "float", "decimal"})
_YEAR_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$|^\d{4}-?q[1-4]$")
_PHONE = re.compile(r"^\+?[\d\s()-]{7,16}$")
_POSTAL = re.compile(r"^\d{5,6}$|^[a-z]\d[a-z] ?\d[a-z]\d$|^[a-z]{1,2}\d{1,2}[a-z]? ?\d[a-z]{2}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")
_URL = re.compile(r"^(https?://|www\.)")
_PRICE_TOKENS = frozenset({"price", "cost", "mrp", "fare", "tariff"})


class _Lex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TypeLexicon(_Lex):
    phrases: list[str]
    strong: list[str]
    weak: list[str]


class SemanticTypeLexicon(_Lex):
    version: str
    types: dict[SemanticType, TypeLexicon]
    gazetteers: dict[SemanticType, list[str]]
    boolean_values: list[str]


def load_lexicon(path: Path = LEXICON_PATH) -> SemanticTypeLexicon:
    return SemanticTypeLexicon.model_validate_json(path.read_text(encoding="utf-8"))


@cache
def default_lexicon() -> SemanticTypeLexicon:
    return load_lexicon()


@dataclass(frozen=True)
class TypedColumns:
    columns: list[ColumnUnderstanding]
    decisions: dict[str, Decision]  # per column; kept for calibration and tie-breaking
    review_flags: list[str]


class SemanticTypeClassifier:
    def __init__(
        self, config: UnderstandingConfig | None = None, lexicon: SemanticTypeLexicon | None = None
    ) -> None:
        self._cfg = config or default_config()
        self._lex = lexicon or default_lexicon()

    @property
    def version(self) -> str:
        return self._lex.version

    def classify(self, profile: ProfileResult) -> TypedColumns:
        names = [c.name for c in profile.columns]
        columns: list[ColumnUnderstanding] = []
        decisions: dict[str, Decision] = {}
        flags: list[str] = []
        for index, column in enumerate(profile.columns):
            decision, signals = self._classify_column(column, index, names, profile.row_count)
            decisions[column.name] = decision
            if decision.abstained:
                flags.append(f"TYPE_UNCERTAIN:{column.name}")
            columns.append(
                ColumnUnderstanding(
                    name=column.name,
                    source_name=column.name,
                    physical_type=column.physical_type,
                    semantic_type=T(decision.label),
                    confidence=decision.confidence,
                    signals=signals,
                )
            )
        return TypedColumns(columns, decisions, flags)

    def _classify_column(
        self, column: ColumnProfile, index: int, siblings: Sequence[str], rows: int
    ) -> tuple[Decision, list[str]]:
        name_scores = self.name_scores(column.name)
        scores: dict[str, float] = {}
        parts: dict[str, dict[str, float]] = {}
        for sem_type in T:
            if sem_type is T.UNKNOWN:
                continue
            signals = {
                "name": name_scores.get(sem_type, 0.0),
                "value": self._value_pattern(sem_type, column),
                "stat": _stat_fit(sem_type, column, rows),
                "context": _context(sem_type, column, index, siblings),
            }
            parts[sem_type.value] = signals
            scores[sem_type.value] = weighted_score(signals, self._cfg.semantic_type.weights)
        decision = decide(scores, self._cfg.semantic_type)
        top = decision.top or ""
        text = [f"top={top} score={decision.score:.3f} margin={decision.margin:.3f}"]
        text += [f"{k}={v:.2f}" for k, v in parts.get(top, {}).items()]
        if decision.abstained:
            text.append(f"abstained: {decision.reason} (runner-up {decision.runner_up})")
        return decision, text

    def name_scores(self, header: str) -> dict[SemanticType, float]:
        toks = tokens(header)
        if "per" in toks:  # denominators after "per" do not vote
            toks = toks[: toks.index("per") + 1]
        scores: dict[SemanticType, float] = {}
        consumed: set[str] = set()
        for sem_type, lex in self._lex.types.items():
            for p in lex.phrases:
                needle = tokens(p)
                if contains_phrase(toks, needle):
                    scores[sem_type] = 1.0
                    consumed.update(needle)
        free = {t for t in toks if t not in consumed}
        last = toks[-1] if toks else ""
        for sem_type, lex in self._lex.types.items():
            for terms, strength in ((lex.strong, 0.9), (lex.weak, 0.5)):
                for term in terms:
                    if term in free:
                        score = strength if term == last else strength * 0.85
                        scores[sem_type] = max(scores.get(sem_type, 0.0), score)
        if toks and toks[0] in {"is", "has"}:
            scores[T.BOOLEAN_FLAG] = 1.0
        if "per" in toks:
            if _PRICE_TOKENS & set(toks):
                scores[T.PRICE] = 1.0
                scores.pop(T.RATE_OR_RATIO, None)
            else:
                scores[T.RATE_OR_RATIO] = 1.0
        return scores

    def _value_pattern(self, t: SemanticType, c: ColumnProfile) -> float:
        n = c.numeric if c.physical_type in NUMERIC else None
        string = c.physical_type in {"string", "mixed"}
        if n is not None:
            if t is T.PERCENTAGE and c.unit_hint == "percent":
                return 1.0  # the source cells carried "%" (stored as fractions)
            return self._numeric_pattern(t, c)
        if t is T.IDENTIFIER and string:
            mixed = c.text.pattern_classes.get("mixed", 0.0) if c.text else 0.0
            return 0.9 if mixed >= 0.9 else 0.3
        if t is T.DATETIME:
            if c.physical_type in {"date", "datetime"}:
                return c.parse_success_ratio
            return _regex(c, "date") if string else 0.0
        if t is T.BOOLEAN_FLAG:
            if c.physical_type == "bool":
                return 1.0
            vocab = set(self._lex.boolean_values)
            return _topk_ratio(c, lambda v: v in vocab) if c.cardinality <= 2 else 0.0
        if not string:
            return 0.0
        if t is T.DATE_PART:
            return _topk_ratio(c, lambda v: bool(_YEAR_MONTH.match(v)))
        if t is T.CURRENCY_AMOUNT:
            return _regex(c, "currency")
        if t is T.PERCENTAGE:
            return _regex(c, "percent")
        if t is T.CATEGORY:
            return 0.8 if c.cardinality <= 50 else 0.5
        if t is T.FREE_TEXT:
            return 0.8 if c.text and c.text.mean_length >= 20 else 0.5
        if t in self._lex.gazetteers:
            places = set(self._lex.gazetteers[t])
            return _topk_ratio(c, lambda v: v in places)
        matchers: dict[SemanticType, Callable[[str], bool]] = {
            T.POSTAL_CODE: lambda v: bool(_POSTAL.match(v)),
            T.EMAIL: lambda v: bool(_EMAIL.match(v)),
            T.URL: lambda v: bool(_URL.match(v)),
            T.PHONE: lambda v: bool(_PHONE.match(v)),
        }
        if t in matchers:
            regex_key = {T.EMAIL: "email", T.URL: "url"}.get(t)
            by_regex = _regex(c, regex_key) if regex_key else 0.0
            return max(by_regex, _topk_ratio(c, matchers[t]))
        return 0.0

    @staticmethod
    def _numeric_pattern(t: SemanticType, c: ColumnProfile) -> float:
        n = c.numeric
        assert n is not None  # noqa: S101  # guarded by the caller
        is_int = c.physical_type == "int"
        non_negative = n.neg_ratio == 0
        match t:
            case T.IDENTIFIER:
                return 0.6 if is_int else 0.0
            case T.DATE_PART if is_int:
                if n.min >= 1900 and n.max <= 2100:
                    return 1.0
                return 0.6 if n.min >= 1 and n.max <= 12 else 0.0
            case T.CURRENCY_AMOUNT:
                return 0.4 if is_int else 0.6
            case T.QUANTITY:
                return (0.6 if is_int else 0.5) if non_negative else 0.0
            case T.COUNT:
                return 0.6 if is_int and non_negative else 0.0
            case T.PRICE:
                return 0.5 if n.min > 0 else 0.0
            case T.RATE_OR_RATIO:
                return 0.7 if n.min >= 0 and n.max <= 1 else 0.4
            case T.PERCENTAGE:
                return 0.5 if n.min >= 0 and n.max <= 100 else 0.0
            case T.CATEGORY:
                return 0.3 if is_int and c.cardinality <= 20 else 0.0
            case T.POSTAL_CODE:
                return 0.3 if is_int and n.min >= 10_000 and n.max <= 999_999 else 0.0
            case _:
                return 0.0


def _regex(c: ColumnProfile, key: str) -> float:
    return c.text.regex_hit_ratios.get(key, 0.0) if c.text else 0.0


def _topk_ratio(c: ColumnProfile, predicate: Callable[[str], bool]) -> float:
    if c.categorical is None or c.categorical.masked or not c.categorical.values:
        return 0.0
    total = sum(v.count for v in c.categorical.values)
    hits = sum(v.count for v in c.categorical.values if predicate(v.value.strip().lower()))
    return hits / total if total else 0.0


def _stat_fit(t: SemanticType, c: ColumnProfile, rows: int) -> float:
    numeric = c.physical_type in NUMERIC and c.numeric is not None
    measures = {T.CURRENCY_AMOUNT, T.QUANTITY, T.COUNT, T.PRICE, T.RATE_OR_RATIO}
    if t is T.IDENTIFIER:
        return c.unique_ratio * (1.0 - c.null_ratio)
    if t is T.DATETIME:
        return 1.0 if c.temporal is not None else 0.0
    if t is T.DATE_PART:
        return 1.0 if c.cardinality <= 60 else 0.0
    if t in measures and not numeric:
        return 0.0
    fixed = {T.QUANTITY: 0.7, T.COUNT: 0.7, T.PRICE: 0.6, T.RATE_OR_RATIO: 0.5}
    if t in fixed:
        return fixed[t]
    if t is T.CURRENCY_AMOUNT:
        return 0.8 if c.unique_ratio > 0.3 else 0.4
    if t is T.PERCENTAGE:
        return 0.7 if numeric else 0.3
    if t is T.CATEGORY:
        small = c.cardinality <= max(50, 0.05 * rows)
        return 1.0 if small and c.unique_ratio < 0.5 else max(0.0, 0.5 - c.unique_ratio)
    if t is T.BOOLEAN_FLAG:
        return 1.0 if c.cardinality == 2 else 0.0
    if numeric:
        return 0.0
    if t is T.FREE_TEXT:
        return c.unique_ratio
    if t in {T.GEO_COUNTRY, T.GEO_REGION, T.GEO_CITY}:
        return 0.6
    return 0.5 if c.unique_ratio > 0.5 else 0.2


def _context(t: SemanticType, c: ColumnProfile, index: int, siblings: Sequence[str]) -> float:
    others = [set(tokens(s)) for s in siblings if s != c.name]
    if t is T.IDENTIFIER:
        return 1.0 if index == 0 and c.unique_ratio >= 0.98 else 0.0
    if t is T.PRICE:
        return 1.0 if any({"quantity", "units"} & o for o in others) else 0.0
    if t is T.DATE_PART:
        parts = {"year", "month", "quarter", "week", "season"}
        return 1.0 if any(parts & o for o in others) else 0.0
    return 0.0
