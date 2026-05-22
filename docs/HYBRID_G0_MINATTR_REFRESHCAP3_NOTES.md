# Hybrid-G0 minimal attribution + refresh_cap3 ablation

Base: `repo60408_g0_ranker_redesign.zip`.

Purpose: preserve the old G0/ranker-redesign behavior while adding attribution counters and a single `refresh_cap3` ablation. This package deliberately avoids the earlier Phase1/Phase1R changes.

## Mainline config

Use:

```bash
cp adasel/config/adaselect_hybrid_g0_baseline.json adasel/config/adaselect.json
```

This is intended to be behavior-equivalent to `adaselect_g0_3_fixed_mainline.json`, except for diagnostics/logging.

## Ablation config

Use:

```bash
cp adasel/config/adaselect_hybrid_g0_refresh_cap3.json adasel/config/adaselect.json
```

This sets:

```json
"g0_oldconf_refresh_max_queries_per_index": 3
```

All other old-conf refresh behavior remains unchanged.

## Added diagnostics

Per-round CSV now includes:

- `graph_rich_query_count`
- `graph_sparse_query_count`
- `role_fallback_query_count`
- `fallback_candidate_count`
- `fallback_selected_count`
- `legacy_supplement_candidate_count`
- `legacy_supplement_selected_count`
- `oldconf_refresh_enabled`
- `oldconf_refresh_positive_only`
- `oldconf_refresh_predicate_only`
- `oldconf_refresh_max_queries_per_index`
- `oldconf_refresh_added`
- `oldconf_refresh_queries`
- `oldconf_refresh_unique_indexes`
- `oldconf_refresh_max_aff`

Note: candidate-level `legacy_supplement_*` is conservatively counted as explicit `SEL_FALLBACK` because graph-sparse legacy supplementation is not reliably separable in this baseline without changing metadata semantics.

## Affinity warning fix

The diagnostic threshold now uses `ceil(0.8 * round_size)` rather than `round(0.8 * round_size)`, and the high-affinity message is logged as info rather than warning. This is diagnostic-only.

## CASES_FILTER fix

`scripts/sweep_adaselect_lambda_wdcg.sh` now explicitly splits `CASES_FILTER` on spaces and normalizes CASE row whitespace, fixing filters like `job random`.

## Run script

```bash
bash scripts/run_hybrid_g0_minattr_ablation.sh
```

This runs:

- `hybrid_g0_baseline`
- `hybrid_g0_refresh_cap3`

for:

- `job random/noisy/shifting`
- `tpchs random/noisy/shifting`

Outputs go to:

```text
runs_hybrid_g0_minattr/
```

## Validation

Checked with:

```bash
python -m compileall -q adasel adaselect_pp util scripts
PYTHONPATH=. pytest -q adaselect_pp/tests/test_structured_selection.py adaselect_pp/tests/test_structured_merge.py adaselect_pp/tests/test_structured_ranker.py
```

Result: 12 tests passed.
