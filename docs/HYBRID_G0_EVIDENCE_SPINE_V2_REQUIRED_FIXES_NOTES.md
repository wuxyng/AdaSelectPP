# Hybrid-G0 Evidence Spine v2 — Required Fixes Bundle

This package extends the prior `repo60408_hybrid_g0_evidence_spine_v2.zip` with three required engineering fixes while preserving the Hybrid-G0/topk-beta mainline.

## Included fixes

1. **DatabaseConnector SQL sanitization**
   - Adds connector-level `_sql_only()` guard.
   - Ensures `get_plan`, `get_query_runtime`, `get_query_cost` via `get_plan`, `execute_only`, `execute_and_fetch`, `exec_fetchall`, `exec_fetchall_params`, and `fetch_one_value` do not send `<template_id>\t<SQL>` to PostgreSQL.
   - This fixes errors such as `EXPLAIN (FORMAT JSON) 7 select ...`.

2. **Benchmark-specific indexable column vocabulary**
   - Adds `adaselect_pp/core/column_vocabulary.py` usage in `CandidateGenerator`.
   - Loads optional files such as `txt/tpch_indexable_columns.txt`, `txt/tpchs_indexable_columns.txt`, and `txt/job_indexable_columns.txt`.
   - Treats these files as a whitelist/vocabulary only; they do not enumerate candidates by themselves.
   - Candidate generation remains evidence-driven: SQL/AST/role evidence activates columns, then the whitelist filters invalid/non-benchmark columns.
   - Records `indexable_vocab_*` stats.

3. **Static SQL/AST cache only**
   - Adds static AST parsing cache only; it never caches EXPLAIN plans.
   - TemplateExtractor uses real template id + normalized SQL signature when available; q0/q1 pseudo ids fall back to exact SQL hash.
   - SqlglotBackend also keeps a small exact-SQL parse cache and returns copies when possible.
   - Template/signature EXPLAIN plan caches remain disabled.

## Still intentionally disabled

- Template-id EXPLAIN plan cache.
- Signature-level EXPLAIN plan cache.
- Compile-validation hard gate.
- Retain/swap mainline.
- Phase1/Phase1R U_keep/U_anchor split-visibility mainline.

## Expected use

Use the same mainline JSON:

```bash
cp adasel/config/adaselect_g0_3_fixed_mainline.json adasel/config/adaselect.json
TRACE=1 LAM_POLICIES=adaptive WDCG_VALUES=1 MIN_WIDTH=1 MAX_WIDTH=2 CASES_FILTER="tpchs noisy" bash scripts/sweep_adaselect_lambda_wdcg.sh
```

If indexable-column files are available in `txt/`, the whitelist is active. If absent, the whitelist is a no-op.
