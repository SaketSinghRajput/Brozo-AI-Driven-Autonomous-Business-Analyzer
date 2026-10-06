"""DomainDetector decisions, evidence and override (TASK-DOMAIN-003..006)."""

from app.data.domain_packs.loader import default_packs
from app.data.domain_packs.model import DomainPack
from app.data.profiling.domain import ProfileResult
from app.data.understanding.analyzer import SchemaAnalyzer, TableInput
from app.data.understanding.config import default_config
from app.data.understanding.domain import DomainStatus, Understanding
from app.data.understanding.domain_detector import DomainDetector
from tests.unit.data.understanding.factories import col, key, numeric, orders_profile, profile, text


def _understand(p: ProfileResult) -> list[Understanding]:
    return [SchemaAnalyzer().analyze([TableInput("t", p)])["t"].understanding]


def _pack(pack_id: str, terms: list[str], compatible: list[str] | None = None) -> DomainPack:
    return DomainPack.model_validate(
        {
            "id": pack_id,
            "version": "1.0.0",
            "name": pack_id,
            "description": "test pack",
            "compatible_with": compatible or [],
            "lexicon": [{"term": t, "weight": 1.0} for t in terms],
            "concepts": [
                {"name": "Thing", "kind": "entity", "semantic_types": ["identifier"],
                 "aliases": ["thing id"]}
            ],
        }
    )  # fmt: skip


def test_retail_like_data_is_known_retail_with_evidence() -> None:
    result = DomainDetector().detect(_understand(orders_profile()))
    assert result.status is DomainStatus.KNOWN
    assert result.primary == "retail"
    kinds = {e.kind for e in result.candidates[0].evidence}
    assert {"lexicon", "signature", "entity_graph", "statistical"} <= kinds
    assert set(result.pack_versions) == set(default_packs())


def test_saas_like_data_is_known_saas() -> None:
    p = profile(
        key("subscription_id"),
        col("plan", card=3),
        col("mrr", "float", unique=0.8, card=800, numeric=numeric(10, 900)),
        col("start_date", "date", card=500),
        col("status", card=2),
    )
    result = DomainDetector().detect(_understand(p))
    assert (result.status, result.primary) == (DomainStatus.KNOWN, "saas")


def test_generic_columns_are_unknown() -> None:
    p = profile(
        key("id"),
        col("category", card=4),
        col("value", "float", unique=0.9, card=900),
        col("amount", "float", unique=0.9, card=900),
        col("date", "date", card=300),
    )
    result = DomainDetector().detect(_understand(p))
    assert result.status is DomainStatus.UNKNOWN
    assert result.primary is None


def test_close_scores_are_ambiguous() -> None:
    packs = {"alpha": _pack("alpha", ["widget"]), "beta": _pack("beta", ["widget"])}
    p = profile(key("widget_id"), col("widget_kind", card=3))
    result = DomainDetector(packs).detect(_understand(p))
    assert result.status is DomainStatus.AMBIGUOUS
    assert result.primary is None
    assert [c.domain for c in result.candidates] == ["alpha", "beta"]


def test_compatible_packs_above_tau_are_multi_domain() -> None:
    packs = {
        "alpha": _pack("alpha", ["widget"], ["beta"]),
        "beta": _pack("beta", ["widget"], ["alpha"]),
    }
    p = profile(key("widget_id"), col("widget_kind", card=3))
    result = DomainDetector(packs).detect(_understand(p))
    assert result.status is DomainStatus.MULTI_DOMAIN
    assert result.primary == "alpha"


def test_below_tau_is_unknown_even_with_candidates() -> None:
    tau = default_config().domain.tau
    packs = {"alpha": _pack("alpha", ["gizmo"])}
    p = profile(key("id"), col("gizmo_note", unique=0.9, card=900, text=text(mean_length=30)))
    result = DomainDetector(packs).detect(_understand(p))
    assert result.status is DomainStatus.UNKNOWN
    assert result.candidates
    assert result.candidates[0].score < tau


def test_override_always_wins() -> None:
    result = DomainDetector().override("agriculture", by="usr_42")
    assert (result.status, result.primary, result.confidence) == (
        DomainStatus.KNOWN, "agriculture", 1.0,
    )  # fmt: skip
    assert result.candidates[0].evidence[0].kind == "user_override"


def test_detection_is_deterministic() -> None:
    u = _understand(orders_profile())
    assert DomainDetector().detect(u) == DomainDetector().detect(u)
