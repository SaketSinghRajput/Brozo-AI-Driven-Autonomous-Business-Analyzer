"""DomainDetector (TASK-DOMAIN-003..006; design 14, TRD 12).

Pipeline (LLM and embeddings disabled; both are P2):
  FeatureExtractor (per scorable pack)
    lexicon      - term hits per column, role-weighted, saturating at ``lexicon_saturation``
    signature    - weighted share of each signature's required concepts present in the data
    entity_graph - share of the pack's entity edges whose two entities are both present
    statistical  - share of stat hints (semantic type + additivity) found among measures
  -> score = weighted mean over the feature groups the pack defines
  -> decide: top < tau -> UNKNOWN; >=2 above tau and declared compatible -> MULTI_DOMAIN;
     margin < delta -> AMBIGUOUS; else KNOWN. Confidence = top score (uncalibrated, ADR-0026:
     no scikit-learn dependency for isotonic calibration yet).
A concept is "present" when a column's header alias-matches it (>= 0.8) and the column's semantic
type is one the concept allows. Every candidate carries its evidence list.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.data.domain_packs.loader import default_packs
from app.data.domain_packs.model import DomainPack
from app.data.semantic.aliases import best_alias
from app.data.understanding.config import UnderstandingConfig, default_config
from app.data.understanding.domain import (
    DomainCandidate,
    DomainClassification,
    DomainEvidence,
    DomainStatus,
    SemanticType,
    Understanding,
)
from app.data.understanding.scoring import rank
from app.data.understanding.text import contains_phrase, tokens

log = logging.getLogger("app.data.domain")
PRESENCE_MIN = 0.8


@dataclass(frozen=True)
class DetectorColumn:
    name: str
    role: str  # measure | identifier | time | dimension | other
    semantic_type: SemanticType
    additivity: str | None


@dataclass(frozen=True)
class PackFeatures:
    values: dict[str, float]  # feature group -> [0, 1]
    evidence: list[DomainEvidence]
    present_concepts: list[str]


class DomainDetector:
    def __init__(
        self,
        packs: Mapping[str, DomainPack] | None = None,
        config: UnderstandingConfig | None = None,
    ) -> None:
        self._packs = dict(packs if packs is not None else default_packs())
        self._cfg = (config or default_config()).domain

    def detect(self, understandings: Sequence[Understanding]) -> DomainClassification:
        columns = detector_columns(understandings)
        scored: dict[str, tuple[float, PackFeatures]] = {}
        for pack in sorted(self._packs.values(), key=lambda p: p.id):
            if not pack.scorable:
                continue
            features = self.features(pack, columns)
            score = self._score(features.values)
            log.debug(
                "domain.features",
                extra={"fields": {"pack": pack.id, "features": features.values, "score": score}},
            )
            if score > 0:
                scored[pack.id] = (round(score, 4), features)
        ranked = rank({pack_id: s for pack_id, (s, _) in scored.items()})
        candidates = [
            DomainCandidate(domain=pack_id, score=score, evidence=scored[pack_id][1].evidence)
            for pack_id, score in ranked
        ]
        return self._decide(candidates)

    def override(self, domain: str, by: str) -> DomainClassification:
        """A user override always wins (design 14)."""
        if domain not in self._packs:
            raise ValueError(f"unknown domain pack {domain!r}")
        evidence = [DomainEvidence(kind="user_override", detail=f"set by {by}", weight=1.0)]
        return DomainClassification(
            status=DomainStatus.KNOWN,
            primary=domain,
            candidates=[DomainCandidate(domain=domain, score=1.0, evidence=evidence)],
            confidence=1.0,
            pack_versions=self._versions(),
        )

    def _score(self, values: Mapping[str, float]) -> float:
        weights = {k: self._cfg.weights.get(k, 0.0) for k in values}
        total = sum(weights.values())
        return sum(weights[k] * v for k, v in values.items()) / total if total else 0.0

    def features(self, pack: DomainPack, columns: Sequence[DetectorColumn]) -> PackFeatures:
        evidence: list[DomainEvidence] = []
        values: dict[str, float] = {}
        if pack.lexicon:
            values["lexicon"] = self._lexicon(pack, columns, evidence)
        present = _present(pack, columns)
        if pack.signatures:
            total = sum(s.weight for s in pack.signatures)
            got = 0.0
            for sig in pack.signatures:
                have = [c for c in sig.requires if c in present]
                share = len(have) / len(sig.requires)
                got += sig.weight * share
                if have:
                    detail = f"signature {sig.id}: {len(have)}/{len(sig.requires)} {have}"
                    evidence.append(
                        DomainEvidence(kind="signature", detail=detail, weight=sig.weight * share)
                    )
            values["signature"] = got / total
        if pack.entity_graph:
            linked = [e for e in pack.entity_graph if e.from_ in present and e.to in present]
            values["entity_graph"] = len(linked) / len(pack.entity_graph)
            evidence += [
                DomainEvidence(kind="entity_graph", detail=f"{e.from_} {e.verb} {e.to}", weight=1.0)
                for e in linked
            ]
        if pack.stat_hints:
            total = sum(h.weight for h in pack.stat_hints)
            got = 0.0
            for hint in pack.stat_hints:
                if any(
                    c.semantic_type is hint.semantic_type
                    and (hint.additivity is None or c.additivity == hint.additivity)
                    for c in columns
                ):
                    got += hint.weight
                    detail = f"stat hint {hint.id}"
                    evidence.append(
                        DomainEvidence(kind="statistical", detail=detail, weight=hint.weight)
                    )
            values["statistical"] = got / total
        return PackFeatures(values, evidence, sorted(present))

    def _lexicon(
        self, pack: DomainPack, columns: Sequence[DetectorColumn], evidence: list[DomainEvidence]
    ) -> float:
        raw = 0.0
        for col in columns:
            col_tokens = tokens(col.name)
            hits = [t for t in pack.lexicon if contains_phrase(col_tokens, tokens(t.term))]
            if not hits:
                continue
            best = max(hits, key=lambda t: (t.weight, t.term))
            weight = best.weight * self._cfg.role_weights.get(col.role, 0.3)
            raw += weight
            detail = f"column {col.name!r} ({col.role}) matches term {best.term!r}"
            evidence.append(DomainEvidence(kind="lexicon", detail=detail, weight=round(weight, 4)))
        return min(1.0, raw / self._cfg.lexicon_saturation)

    def _decide(self, candidates: list[DomainCandidate]) -> DomainClassification:
        versions = self._versions()
        top_score = candidates[0].score if candidates else 0.0
        if top_score < self._cfg.tau:
            return DomainClassification(
                status=DomainStatus.UNKNOWN,
                primary=None,
                candidates=candidates,
                confidence=top_score,
                pack_versions=versions,
            )
        top = candidates[0]
        above = [c for c in candidates if c.score >= self._cfg.tau]
        second = candidates[1].score if len(candidates) > 1 else 0.0
        status: DomainStatus = DomainStatus.KNOWN
        primary: str | None = top.domain
        if len(above) >= 2 and above[1].domain in self._packs[top.domain].compatible_with:
            status = DomainStatus.MULTI_DOMAIN
        elif top.score - second < self._cfg.delta:
            status, primary = DomainStatus.AMBIGUOUS, None
        return DomainClassification(
            status=status,
            primary=primary,
            candidates=candidates,
            confidence=top.score,
            pack_versions=versions,
        )

    def _versions(self) -> dict[str, str]:
        return {p.id: p.version for p in sorted(self._packs.values(), key=lambda p: p.id)}


def _present(pack: DomainPack, columns: Sequence[DetectorColumn]) -> set[str]:
    present = set()
    for concept in pack.concepts:
        for col in columns:
            if col.semantic_type not in concept.semantic_types:
                continue
            score, _ = best_alias(col.name, [concept.name, *concept.aliases])
            if score >= PRESENCE_MIN:
                present.add(concept.name)
                break
    return present


def detector_columns(understandings: Sequence[Understanding]) -> list[DetectorColumn]:
    out = []
    for u in understandings:
        measures = {m.column: m.additivity for m in u.measures}
        times = {t.column for t in u.time_dimensions}
        dims = {d.column for d in u.dimensions}
        for c in u.columns:
            if c.name in measures:
                role = "measure"
            elif c.semantic_type is SemanticType.IDENTIFIER:
                role = "identifier"
            elif c.name in times:
                role = "time"
            elif c.name in dims:
                role = "dimension"
            else:
                role = "other"
            out.append(DetectorColumn(c.name, role, c.semantic_type, measures.get(c.name)))
    return out
