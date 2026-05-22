# G0-3 ablation experiment design

## Goal

Measure whether G0-3 improves **execution-side quality** on top of the already-strong G0-2 mainline, and whether Phase 0.5 compile validation produces net benefit rather than pure overhead.

This design uses a clean 3x2 factorial:

- Merge mode: `none`, `group/order`, `group/order+covering`
- Compile validation: `off`, `on`

Small-table and high-DML table filtering stay **on for all variants** so the experiment stays on the intended production mainline.

## Primary workloads

Run with `TRACE=1`, `lambda_policy=adaptive`, `wdcg_enabled=true`, `wdcg_enum_mode=g0_selection`.

Primary cases:
- `tpch_random`
- `tpchs_random`
- `tpchs_noisy`
- `tpchs_shifting`

Optional secondary sanity extension:
- `tpch_noisy`
- `tpch_shifting`

## Variants

- `A0_g0_2_base_no_compile`: G0-2 base only
- `A1_g0_2_base_with_compile`: isolate compile-validation effect on base G0-2 candidates
- `B0_g0_3_go_no_compile`: add only group/order merge
- `B1_g0_3_go_with_compile`: group/order merge + compile validation
- `C0_g0_3_goc_no_compile`: add group/order + covering merge
- `C1_g0_3_goc_with_compile`: full G0-3 + Phase 0.5 mainline candidate flow

## Hypotheses

### A1 vs A0
Tests whether compile validation is already useful on pure G0-2 candidates.

Expected:
- `exec_avg`: roughly flat
- `cand_sum`: flat
- `compile_validated / trials`: high
- `total_avg`: flat to slightly worse unless compile gate removes some bad tails

Interpretation:
- If A1 is much worse than A0, compile gate is too expensive or too strict.
- If A1 is slightly better, compile gate has standalone value.

### B0 vs A0
Tests whether group/order merge can reduce execution time without compile help.

Expected:
- `exec_avg`: improves first on `tpchs_shifting` and `tpchs_noisy`
- `cand_sum`: small increase
- `merged_group + merged_order`: clearly non-zero

Interpretation:
- If `exec_avg` does not improve on any tpchs case, group/order merge is not earning its keep.

### B1 vs B0
Tests whether compile validation helps control bad group/order merges.

Expected:
- `compile_not_picked`: non-zero
- `compile_validated / trials`: moderate, not extreme
- `total_avg`: should improve or stay near-flat relative to B0

Interpretation:
- If `compile_not_picked` is near zero, merge is already very clean or compile gate is not seeing enough.
- If `compile_not_picked` is very high and `exec_avg` does not improve, merge rules are too loose.

### C0 vs B0
Tests whether covering merge adds real execution value beyond group/order.

Expected:
- biggest upside on `tpchs_noisy` / `tpchs_shifting`
- `width_after_merge=3` share rises, but should stay controlled

Interpretation:
- If covering raises transition/recommendation cost but not `exec_avg`, keep it disabled in the mainline.

### C1 vs C0
Tests whether compile validation makes covering safe enough for production.

Expected:
- `compile_not_picked` rises versus B1
- `total_avg` should improve versus C0 if compile gate is filtering bad covers

Interpretation:
- If C1 still regresses, covering is not ready even with Phase 0.5.

## Success criteria

### Hard reject criteria
Reject a variant if either of these happens:
- `total_avg` regresses by more than **5%** on any primary tpchs workload relative to A0
- `timeout_count` increases materially on any primary workload

### Promote criteria
Promote a variant if all of these hold:
- average `exec_avg` improves on at least **2 primary workloads**
- overall `total_avg` is not worse than A0 by more than **2%** on any primary workload
- merge-added candidates are actually used: `merged_total > 0` and `width_after_merge=3` appears in final `new`
- compile gate is informative when enabled: `compile_not_picked > 0` but `compile_validated / trials` is not trivially low

### Covering-specific gate
Only keep covering enabled if:
- `C1` beats `B1` on at least one tpchs workload by **>=2% exec_avg** or **>=1.5% total_avg**
- and `compile_not_picked / trials` for covering merges does not explode

## Metrics to compare

Use summary, per-round CSV, and trace together.

### Summary level
- `exec_avg`
- `total_avg`
- `trans_avg`
- `cand_sum`
- `eval_sum`
- `what_if_sum`
- `timeout_count`

### Per-round CSV
- `merged_total`
- `merged_group`
- `merged_order`
- `merged_covering`
- `compile_validation_enabled`
- `compile_validation_passes`
- `compile_validation_trials`
- `compile_validated`
- `compile_invalidated`
- `compile_not_picked`
- `wdcg_selected_post_compile`
- `skipped_high_dml_tables`
- `pruned_small_tables`

### TRACE
- `family`
- `base_family`
- `merge_family`
- `merge_suffix_source`
- `compile_valid`
- `compile_pick_reason`
- `skip_reason`
- `width_before_merge`
- `width_after_merge`
- `table_row_count`
- `table_dml_ratio`
- `lambda`
- `lambda_shadow`

## Run order

Run in this order to fail fast:

1. `A0` and `A1`
2. `B0` and `B1`
3. `C0` and `C1`

After each pair, check tpchs workloads before proceeding.

## Recommended shell pattern

Example for one variant:

```bash
TRACE=1 python -m adasel.main   --algo AdaSelect   --config adasel/config/<VARIANT>.json   --workload database/workload/tpchs_noisy.txt   --wdcg 1
```

## Decision logic

- If `B1` beats `A0` and `C1` does not beat `B1`, mainline should be **G0-2 + group/order + compile gate**.
- If `C1` clearly beats `B1`, mainline can move to **full G0-3 + Phase 0.5**.
- If `A1` already hurts badly, fix compile validation overhead before trusting any merge-on result.
