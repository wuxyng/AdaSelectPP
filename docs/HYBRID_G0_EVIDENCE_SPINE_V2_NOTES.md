# Hybrid-G0 Evidence Spine v2

Base: `repo60408_g0_ranker_redesign.zip`.

This package removes the hidden plan-cache/fail-open instability and makes the current Hybrid-G0 baseline deterministic enough for diagnosis, without reintroducing Phase 1/Phase 1R or retain/swap.

## What changed

1. **Preserve real template ids**
   - `adasel/main.py::load_workloads()` now normalizes workload rows to `<template_id>\t<SQL>` instead of discarding the template id.
   - Runtime execution strips the template prefix before sending SQL to PostgreSQL.
   - `database/cost_evaluation.py` also strips the prefix before cost estimation.

2. **Disable unsafe EXPLAIN plan cache**
   - `TemplateExtractor` no longer caches EXPLAIN plans by pseudo template id or normalized signature.
   - Exact SQL plan cache is disabled by default as well because benchmark SQLs rarely repeat exactly and plans are configuration-dependent.
   - Template id is retained only as a semantic label, not a plan-cache key.

3. **Fix AST runtime error**
   - Added missing `import collections` in `template_extractor.py`.

4. **Add bare-column AST resolution**
   - Unqualified SQL columns are resolved only when exactly one table instance in the query schema owns the column.

5. **Add candidate-vacuum rescue**
   - After PK/UNIQUE filtering, if a query/table had generated candidates before fixed-index filtering but has no viable candidate after filtering, one conservative non-fixed role candidate is rescued.
   - This targets cases like `supplier(s_suppkey)` being filtered as PK while `supplier(s_nationkey)` is the viable conservative fallback.
   - This is not legacy co-occurrence generation; it is a table-level viability invariant.

6. **Disable compile hard gate**
   - `phase0_compile_validation` no longer filters candidates. If enabled, it records that the hard gate is disabled and returns selected candidates unchanged.
   - The mainline remains `phase0_compile_validation=false`.

7. **Fix HypoPG metadata API**
   - `get_virtual_index_metadata()` now uses `index_name` and `hypopg_get_indexdef(indexrelid)` instead of obsolete/mismatched probes such as `indexdef`, `indexname` on `hypopg_list_indexes`, or `hypopg_display_index()`.

8. **Fix CASES_FILTER script bug**
   - `scripts/sweep_adaselect_lambda_wdcg.sh` now explicitly splits `CASES_FILTER` on spaces and normalizes CASE row whitespace.

9. **Fix high-affinity diagnostic threshold**
   - Uses `ceil(0.8 * round_size)` instead of `round(...)`.
   - Logs high-affinity diagnostics as `INFO`, not `WARNING`.

10. **Expose graph route stats**
   - Per-round stats include graph shadow route counters such as `graph_shadow_attempts`, `graph_shadow_success`, `graph_shadow_fail`, `graph_shadow_ast_fail`.

## What did not change

- No Phase 1 / Phase 1R logic.
- No retain/swap mainline.
- No U_keep/U_anchor split-visibility implementation.
- No compile-validation hard filtering.
- No removal of role fallback or old-conf relevance refresh.
- Topk-beta remains the main configuration policy.
- Old G0 timeout policy remains the mainline behavior.

## Recommended first tests

Use the existing mainline config:

```bash
cp adasel/config/adaselect_g0_3_fixed_mainline.json adasel/config/adaselect.json
```

Run small first-pass cases:

```bash
TRACE=1 LAM_POLICIES=adaptive WDCG_VALUES=1 MIN_WIDTH=1 MAX_WIDTH=2 CASES_FILTER="tpchs noisy" bash scripts/sweep_adaselect_lambda_wdcg.sh
TRACE=1 LAM_POLICIES=adaptive WDCG_VALUES=1 MIN_WIDTH=1 MAX_WIDTH=2 CASES_FILTER="tpchs random" bash scripts/sweep_adaselect_lambda_wdcg.sh
TRACE=1 LAM_POLICIES=adaptive WDCG_VALUES=1 MIN_WIDTH=1 MAX_WIDTH=2 CASES_FILTER="job random" bash scripts/sweep_adaselect_lambda_wdcg.sh
```

Watch these fields:

- `candidate_count_raw`
- `candidate_count`
- `evaluated_count`
- `what_if_calls`
- `candidate_vacuum_rescue_added`
- `graph_shadow_success`, `graph_shadow_fail`, `graph_shadow_ast_fail`
- final `new` configuration

