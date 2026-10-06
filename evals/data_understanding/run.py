"""Data-understanding eval harness (TASK-EVAL-001, TASK-DOMAIN-008).

    cd backend && uv run python ../evals/data_understanding/run.py [--check] [--write-baseline]

Runs profile (test-only CSV helper) -> SchemaAnalyzer -> DomainDetector -> SemanticMapper over
every labelled fixture in evals/fixtures (messy/ and hostile/ are ingestion fixtures and skipped),
compares with labels.json and writes evals/reports/data_understanding.json. ``--check`` fails
(exit 1) when any metric is worse than evals/baselines/data_understanding.json.

Deterministic: same code + config + fixtures -> byte-identical report.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(HERE))

from calibration import ece, sweep  # noqa: E402
from csv_profile import profile_csv  # noqa: E402

from app.data.semantic.mapper import SemanticMapper  # noqa: E402
from app.data.understanding.analyzer import SchemaAnalyzer, TableInput  # noqa: E402
from app.data.understanding.config import UnderstandingConfig, default_config  # noqa: E402
from app.data.understanding.domain import DomainStatus, Understanding  # noqa: E402
from app.data.understanding.domain_detector import DomainDetector  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures"
REPORT = ROOT / "evals" / "reports" / "data_understanding.json"
BASELINE = ROOT / "evals" / "baselines" / "data_understanding.json"
SKIP = {"messy", "hostile"}
PRECISION_FLOOR = 0.95
DEFAULT_CURRENCY = "INR"  # workspace default currency of the synthetic fixtures
_CONFIRMED = {DomainStatus.KNOWN, DomainStatus.MULTI_DOMAIN}


@dataclass(frozen=True)
class _Counter:
    rows: list[dict[str, str]]

    def count_distinct(self, columns: Sequence[str]) -> int:
        return len({tuple(r[c] for c in columns) for r in self.rows})


def _role(u: Understanding, column: str) -> str:
    if column in {m.column for m in u.measures}:
        return "measure"
    if column in {t.column for t in u.time_dimensions}:
        return "time"
    if column in {d.column for d in u.dimensions}:
        return "dimension"
    keys = {c for e in u.entities for c in e.key_columns}
    return "identifier" if column in keys else "none"


def _unit_ok(predicted: str, expected: str) -> bool:
    # label "unknown" = the file carries no currency; any currency unit is acceptable there
    return predicted == expected or (expected == "unknown" and predicted.startswith("currency"))


def _domain(labels: dict[str, Any], status: DomainStatus, primary: str | None) -> tuple[bool, str]:
    exp = labels["domain"]
    if exp["expected_status"] == "known":
        allowed = {exp["primary"], *exp.get("acceptable_alternatives", [])}
        return status is DomainStatus.KNOWN and primary in allowed, exp["primary"]
    statuses = {
        s.upper() for s in [exp["expected_status"], *exp.get("acceptable_alternatives_status", [])]
    }
    return status.value in statuses, "unknown"


def _evaluate_fixture(name: str, cfg: UnderstandingConfig) -> dict[str, Any]:
    labels = json.loads((FIXTURES / name / "labels.json").read_text(encoding="utf-8"))
    profiled = {
        t: profile_csv(FIXTURES / name / f"{t}.csv", f"dsv_{name}_{t}") for t in labels["tables"]
    }
    inputs = [
        TableInput(t, p.profile, distinct=p.distinct.__getitem__, counter=_Counter(p.rows))
        for t, p in profiled.items()
    ]
    currency = labels.get("currency", DEFAULT_CURRENCY)
    analysis = SchemaAnalyzer(cfg).analyze(inputs, default_currency=currency)
    domain = DomainDetector(config=cfg).detect([a.understanding for a in analysis.values()])
    mapper = SemanticMapper(config=cfg)
    columns, decisions, semantic = [], [], {}
    for table, tlabels in labels["tables"].items():
        u = analysis[table].understanding
        mapped = mapper.map(u, domain, table=table)
        semantic[table] = {
            "metrics": [m.name for m in mapped.model.metrics],
            "review_flags": mapped.review_flags + u.review_flags,
        }
        measures = {m.column: m for m in u.measures}
        by_col = {c.name: c for c in u.columns}
        for col, lab in tlabels["columns"].items():
            m = measures.get(col)
            columns.append(
                {
                    "column": f"{table}.{col}",
                    "type_ok": by_col[col].semantic_type.value == lab["semantic_type"],
                    "role_ok": _role(u, col) == lab["role"],
                    "additivity_ok": (m.additivity if m else "n/a") == lab["additivity"],
                    "unit_ok": _unit_ok((m.unit or "unitless") if m else "unitless", lab["unit"]),
                    "predicted": by_col[col].semantic_type.value,
                    "expected": lab["semantic_type"],
                }
            )
            d = analysis[table].type_decisions[col]
            decisions.append((d.score, d.margin, d.top == lab["semantic_type"]))
    entity_hits = [
        any(e.key_columns == ent["key"] for e in analysis[ent["table"]].understanding.entities)
        for ent in labels["entities"]
    ]
    expected_rel = {
        (f"{r['from_table']}.{r['from_column']}", f"{r['to_table']}.{r['to_column']}")
        for r in labels["relationships"]
    }
    found_rel = {(r.from_, r.to) for a in analysis.values() for r in a.understanding.relationships}
    ok, expected_label = _domain(labels, domain.status, domain.primary)
    return {
        "columns": columns,
        "decisions": decisions,
        "entities": entity_hits,
        "relationships": (len(expected_rel), len(found_rel), len(expected_rel & found_rel)),
        "semantic": semantic,
        "domain": {
            "status": domain.status.value,
            "primary": domain.primary,
            "confidence": domain.confidence,
            "candidates": {c.domain: c.score for c in domain.candidates},
            "ok": ok,
            "expected_label": expected_label,
            "predicted_label": domain.primary if domain.status in _CONFIRMED else "unknown",
        },
    }


def _decision_confidence(d: dict[str, Any]) -> float:
    asserted = d["predicted_label"] != "unknown"
    return float(d["confidence"]) if asserted else round(1.0 - float(d["confidence"]), 4)


def _ratio(values: Sequence[bool]) -> float:
    return round(sum(values) / len(values), 4) if values else 1.0


def macro_f1(pairs: Sequence[tuple[str, str]]) -> float:
    labels = sorted({label for pair in pairs for label in pair})
    scores = []
    for label in labels:
        tp = sum(1 for e, p in pairs if e == label and p == label)
        fp = sum(1 for e, p in pairs if e != label and p == label)
        fn = sum(1 for e, p in pairs if e == label and p != label)
        scores.append(2 * tp / (2 * tp + fp + fn))
    return round(sum(scores) / len(scores), 4) if scores else 1.0


def run(cfg: UnderstandingConfig | None = None) -> dict[str, Any]:
    cfg = cfg or default_config()
    names = sorted(
        p.name for p in FIXTURES.iterdir() if (p / "labels.json").exists() and p.name not in SKIP
    )
    per = {name: _evaluate_fixture(name, cfg) for name in names}
    cols = [c for r in per.values() for c in r["columns"]]
    decisions = [d for r in per.values() for d in r["decisions"]]
    expected, found, correct = (sum(r["relationships"][i] for r in per.values()) for i in range(3))
    domains = [r["domain"] for r in per.values()]
    metrics = {
        "type_accuracy": _ratio([c["type_ok"] for c in cols]),
        "type_ece": ece([(score, ok) for score, _, ok in decisions]),
        "role_accuracy": _ratio([c["role_ok"] for c in cols]),
        "additivity_accuracy": _ratio([c["additivity_ok"] for c in cols]),
        "unit_accuracy": _ratio([c["unit_ok"] for c in cols]),
        "mapping_accuracy": _ratio(
            [c["role_ok"] and c["additivity_ok"] and c["unit_ok"] for c in cols]
        ),
        "entity_recall": _ratio([h for r in per.values() for h in r["entities"]]),
        "relationship_precision": round(correct / found, 4) if found else 1.0,
        "relationship_recall": round(correct / expected, 4) if expected else 1.0,
        "domain_accuracy": _ratio([d["ok"] for d in domains]),
        "domain_macro_f1": macro_f1([(d["expected_label"], d["predicted_label"]) for d in domains]),
        # confidence in the decision itself: top score when a domain is asserted, else 1 - top
        "domain_ece": ece([(_decision_confidence(d), d["ok"]) for d in domains]),
    }
    return {
        "config_version": cfg.version,
        "thresholds": {
            "semantic_type": {"tau": cfg.semantic_type.tau, "delta": cfg.semantic_type.delta},
            "domain": {"tau": cfg.domain.tau, "delta": cfg.domain.delta},
            "mapping": {"tau": cfg.mapping.tau, "delta": cfg.mapping.delta},
        },
        "calibration": "none (ADR-0026: scikit-learn is not a backend dependency; raw scores)",
        "precision_floor": PRECISION_FLOOR,
        "type_threshold_sweep": sweep(
            decisions, PRECISION_FLOOR, (cfg.semantic_type.tau, cfg.semantic_type.delta)
        ),
        "metrics": metrics,
        "type_errors": [c for c in cols if not c["type_ok"]],
        "domains": dict(zip(per, domains, strict=True)),
        "semantic": {name: r["semantic"] for name, r in per.items()},
    }


def summary(report: dict[str, Any]) -> str:
    lines = ["| metric | value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in report["metrics"].items()]
    lines += ["", "| fixture | status | primary | confidence | ok |", "|---|---|---|---|---|"]
    for name, d in report["domains"].items():
        lines.append(f"| {name} | {d['status']} | {d['primary']} | {d['confidence']} | {d['ok']} |")
    swept = report["type_threshold_sweep"]
    floor = report["precision_floor"]
    lines.append(f"\nsemantic-type sweep (precision floor {floor}): best {swept['best']}")
    lines.append(f"current config point: {swept['current']}")
    return "\n".join(lines)


def check(report: dict[str, Any], baseline_path: Path = BASELINE) -> list[str]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["metrics"]
    lower_is_better = {"type_ece", "domain_ece"}
    failures = []
    for key, base in baseline.items():
        now = report["metrics"].get(key)
        if now is None:
            failures.append(f"{key}: missing")
        elif (now > base + 1e-9) if key in lower_is_better else (now < base - 1e-9):
            failures.append(f"{key}: {now} vs baseline {base}")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data-understanding eval harness")
    parser.add_argument("--check", action="store_true", help="fail on regression vs baseline")
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)
    report = run()
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8", newline="\n")
    if args.write_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(text, encoding="utf-8", newline="\n")
    print(summary(report))
    if args.check:
        failures = check(report)
        for failure in failures:
            print(f"REGRESSION {failure}")
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
