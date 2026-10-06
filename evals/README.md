# evals

`evals/fixtures/`: the labelled benchmark datasets (TASK-TEST-001/002). Later: evaluation harnesses (`data_understanding/`, `llm/`, `rag/`, `validator/`, `regression/`).

Owners: DATA-ML (domain fixtures) and AI-VALIDATION (messy/hostile fixtures, harnesses). Fixture data is synthetic and generated from seeded scripts, so it is reproducible.

## Data-understanding harness (TASK-EVAL-001 / DOMAIN-008)

```
cd backend && uv run python ../evals/data_understanding/run.py            # report
cd backend && uv run python ../evals/data_understanding/run.py --check    # fail on regression
cd backend && uv run python ../evals/data_understanding/run.py --write-baseline
```

Writes `evals/reports/data_understanding.json` (type / role / additivity / unit / mapping accuracy,
entity + relationship recall, domain accuracy + macro-F1, ECE, τ/δ coverage-precision sweep).
Baseline: `evals/baselines/data_understanding.json`. `csv_profile.py` is a TEST-ONLY stand-in for
the real profiler. CI runs it through `backend/tests/unit/data/understanding/test_eval_harness.py`.

## Shared plumbing

`pipeline.py`: a CSV understood through the production path without a database (ingest ->
profile -> understand -> domain -> semantic mapping, stage timings) and `rig()`, one real
`Orchestrator` turn over that table with in-memory evidence/validation/recorder (from
`backend/tests/unit/orchestrator/fakes.py`). Everything below builds on it.

## Golden set (TASK-EVAL-002)

`golden/` — 95 versioned, hashed questions; see `golden/README.md` (incl. known product gaps).

## LLM + grounding harness (TASK-EVAL-003 / EVAL-004)

```
cd backend && uv run python ../evals/llm/run.py                       # FakeLLM smoke (CI)
cd backend && uv run python ../evals/llm/run.py --live [--model mistral:7b] [--few-shot] [--sample 24]
```

Scores tool selection, argument exact/AST match, numbers, structured-output validity, refusals,
injection resistance, latency p50/p95, tokens (`llm/run.py`) and grounding from the stored
claims/evidence/validation of each turn (`llm/grounding.py`); deterministic matching only, no
LLM judge (ADR-0025). Reports: `reports/llm_<label>.json`. `llm/fewshot/` is the few-shot variant
of the `intent.resolve` prompt (`--few-shot`). CI: `backend/tests/unit/evals/`.

## Regression golden outputs (TASK-TEST-008)

```
cd backend && uv run python ../evals/regression/run.py            # check
cd backend && uv run python ../evals/regression/run.py --write    # accept an intended change
```

Snapshots per fixture in `golden/regression/`: semantic model, capability report, default plan,
every metric's total. CI: `backend/tests/unit/evals/test_regression.py`.
