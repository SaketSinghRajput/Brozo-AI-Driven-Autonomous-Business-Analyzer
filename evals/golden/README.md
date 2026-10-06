# evals/golden — golden question sets (TASK-EVAL-002)

95 questions over five fixture tables (retail 32, saas 23, agriculture 21, insufficient 14,
ambiguous/unknown-domain 5), stratified by intent: metric_value, trend, compare, rank,
explain_change, forecast, anomaly, simulate, out_of_scope, unknown_name, clarify, injection.

```
cd backend && uv run python ../evals/golden/build.py           # regenerate *.jsonl + MANIFEST.json
cd backend && uv run python ../evals/golden/build.py --check   # exit 1 when stale
```

- `spec.py` is the only hand-written part: question, stratum, the intent a correct model should
  produce (semantic names as the production SemanticMapper names them), the expected outcome
  (`answered` / `refused` + acceptable refusal codes / `clarify`) and `known_gap`.
- `build.py` computes everything else deterministically: `post_check` -> `QuestionPlanner` ->
  every plan step through the real `ToolExecutor` on the fixture table understood through the
  production ingest -> profile -> understand -> map path (`evals/pipeline.py`). Tool names,
  arguments and expected numbers (`expected.numbers[].value`, rounded to 6 dp) come from the
  analytics tools, never from a human. `current_outcome` is what the build does today.
- `MANIFEST.json`: golden version, SHA-256 of each file, of `spec.py` and of the fixture CSVs.
- CI: `backend/tests/unit/evals/test_golden_llm_smoke.py` (staleness + FakeLLM smoke).

## Known gaps (items with `known_gap`, reported separately by the harness)

| Gap | Items | Where |
|---|---|---|
| Two time dimensions (SaaS Start/End Date): no `time_dimension` is ever passed, dated questions fail with SCHEMA_ERROR | saas-002, 007-010, 014 | `app/orchestrator/planner.py` `_steps` (`resolver.time_dimension(None)`); `IntentOutput` has no time-dimension field |
| Integer year time column: the compiler casts `year` AS DATE, DuckDB `BIGINT -> DATE` conversion error | 10 agriculture items | `app/analytics/sql/compiler.py:123`, `:262` |
| `count()` (generic pack `Record Count`) accepted by the semantic grammar, rejected by the analytics expression compiler | ambiguous-001, 002 | `app/analytics/sql/expr.py:146` vs `app/data/semantic/expression.py:30`, `domain_packs/packs/generic.v1.json:19` |
| Year-over-year comparison is not expressible (always previous period) | insufficient-006 | `app/orchestrator/planner.py` (`compare_to: previous_period`) |

Note: the retail pack defines Revenue as `sum(Quantity * Unit Price)` (gross), while the fixture
labels (`retail/labels.json` `total_revenue_by_month`) are net of `discount_pct`. Golden numbers
follow the product definition (e.g. Aug 2026: 52,657.29 vs label 50,720.54).
