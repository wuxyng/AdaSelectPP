# Hybrid-G0 Evidence Spine v2 plus safety fixes

This package is based on `repo60408_hybrid_g0_evidence_spine_v2.zip` and adds the three required fixes discussed on 2026-05-16:

1. **DatabaseConnector SQL-only guard**
   - All SQL sent to PostgreSQL through `get_plan`, `get_query_runtime`, `get_query_cost`, and workload-cost paths is stripped of workload template prefixes such as `<template_id>\t<SQL>`.
   - This prevents server errors like `EXPLAIN (FORMAT JSON) 7 select ...`.

2. **Benchmark indexable-column vocabulary whitelist**
   - Added `adaselect_pp/core/column_vocabulary.py`.
   - `CandidateGenerator` loads `txt/<benchmark>_indexable_columns.txt` when present.
   - The vocabulary is a passive whitelist: SQL/AST/role evidence must still activate a column; the whitelist only removes evidence for columns outside the benchmark-specific candidate universe.
   - No file found => no-op.
   - Applies before merge, after merge, after fixed-index filtering, and after candidate-vacuum rescue.
   - Adds stats: `indexable_vocab_enabled`, `indexable_vocab_removed`, `indexable_vocab_tables`, `indexable_vocab_columns`, `indexable_vocab_path`.

3. **Static SQL/AST parse cache, no EXPLAIN plan cache**
   - `SqlglotBackend` now caches parse trees by exact `(dialect, SQL text)` and returns copies.
   - This only caches static AST structures. It does **not** cache EXPLAIN plans or planner-derived costs.
   - Template/signature EXPLAIN cache remains disabled.

This version keeps the mainline as:

```text
Hybrid-G0 fallback/ranker baseline
+ candidate-vacuum rescue
+ topk-beta
+ old timeout policy
+ compile hard gate disabled
```

It does not re-enable retain/swap, Phase1/Phase1R, U_keep/U_anchor, G0-3 merge claims, or compile validation hard filtering.
