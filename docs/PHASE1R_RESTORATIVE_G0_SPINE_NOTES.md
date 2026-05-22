# Phase 1R: Restorative G0 Spine

This patch restores three capabilities that the clean Phase-1 spine accidentally removed while keeping the G0 mainline explicit and source-aware.

## What changed

1. `U_anchor`: bounded historical memory lane
   - `U_base = U_curr ∪ U_keep ∪ U_anchor`
   - Anchors are selected from positive `mu_table` / `columns_benefit` entries.
   - Constraints: base-only, width <= 2, positive mu, optional active-table requirement, per-table and global caps.
   - This replaces the old hidden `columns_benefit` action-space behavior with an explicit source lane.

2. Incumbent / anchor relevance refresh
   - Old/keep/anchor keys are refreshed into the per-query relevance map only when their leading column appears in the current query's role evidence.
   - This restores the useful old-conf leading-column refresh without using legacy cooccurrence as a candidate generator.

3. Coverage rescue
   - After source-aware selection, if a query has no selected supported key, the best explicit current/refresh candidate for that query is rescued.
   - This restores workload-level coverage guardrails without reintroducing legacy family generation.

4. Mainline policy
   - Phase 1R mainline uses `choose_policy = topk_beta` by default.
   - `retain_swap` remains available as an ablation/proposal policy, but not as the mainline until oracle/proposal-level analysis is complete.

## Key new config knobs

```json
"g0_anchor_enabled": true,
"g0_anchor_max_width": 2,
"g0_anchor_global_cap": 4,
"g0_anchor_per_table_cap": 1,
"g0_anchor_min_mu": 1e-9,
"g0_anchor_active_table_only": true,
"g0_refresh_enabled": true,
"g0_refresh_include_anchor": true,
"g0_refresh_include_keep": true,
"g0_coverage_rescue_enabled": true,
"g0_selector_keep_cap": 2,
"g0_selector_anchor_cap": 2,
"choose_policy": "topk_beta"
```

## Validation

- `python -m compileall -q adasel adaselect_pp scripts util`
- `PYTHONPATH=. pytest -q adaselect_pp/tests/test_phase1_base_spine.py adaselect_pp/tests/test_retain_swap_choose_config.py adaselect_pp/tests/test_structured_selection.py`
- Result: `18 passed`
