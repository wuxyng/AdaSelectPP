# G0 Phase 1 Experiment Design

## Goal
Validate the first clean G0 base spine:

- `U_curr` = current structured base candidates
- `U_keep` = currently installed base indexes as legal state-visible actions
- no legacy recall patch
- no merge
- no compile validation

The key question is whether `U_keep` reduces catastrophic forgetting and timeout regressions on skewed workloads without reintroducing crowd-out.

## Variants

### V0: curr-only
Config: `adasel/config/adaselect_g0_phase1_curr_only.json`

Semantics:
- `U_base = U_curr`
- no state-visible incumbents

### V1: curr+keep (mainline)
Config: `adasel/config/adaselect_g0_phase1_mainline.json`

Semantics:
- `U_base = U_curr ∪ U_keep`
- current physical configuration contributes legal base actions

## Fixed settings
- `lambda_policy=adaptive`
- `wdcg=1`
- `TRACE=1` for bring-up pass
- `max_width=2`
- `g0_merge_enabled=false`
- `phase0_compile_validation=false`
- `wdcg_graph_fail_open=false`
- `wdcg_graph_ast_backend=sqlglot`
- `wdcg_use_plan=true`

## Primary workloads
- `tpch random`
- `tpchs random`
- `tpchs noisy`
- `tpchs shifting`

## Why these workloads
- `tpchs_*` is the real target: skewed data (Zipfian factor 4) makes state retention matter more.
- `tpch random` is the sanity workload to ensure the new state-visible action space does not destabilize the cleaner base path.

## Primary metrics
Per summary:
- `exec_avg`
- `total_avg`
- `timeout_count`
- `candidate_count`
- `evaluated_count`
- `what_if_calls`

New per-round columns:
- `curr_union`
- `keep_only_union`
- `selected_curr`
- `selected_keep`
- `selected_keep_enabled`

## Trace-level checks
1. No `SEL_FALLBACK` should appear.
2. `selected_keep > 0` should begin to appear after the first few rounds in the mainline.
3. `new` should remain a subset of `appearing ∪ old_conf` by construction.
4. No merge-related fields should fire on the mainline (`merged_*` all zero).

## Hypotheses
### H1 (must hold)
`curr+keep` does not increase timeout count on `tpch random`.

### H2 (main target)
`curr+keep` reduces or eliminates timeout spikes on at least one of:
- `tpchs noisy`
- `tpchs shifting`

### H3 (secondary)
`candidate_count` and `what_if_calls` remain in the same order of magnitude as `curr-only`.
The purpose of `U_keep` is state visibility, not reopening a large candidate universe.

## Acceptance rules
Promote Phase 1 mainline only if all hold:
1. `tpch random`: `total_avg` regression <= 3%
2. `tpchs noisy` and `tpchs shifting`: timeout count does not increase versus curr-only
3. At least one `tpchs_*` workload shows strictly better `exec_avg` or fewer timeouts
4. Trace shows non-zero `selected_keep` in mainline and zero in curr-only

## Suggested run order
1. Single-case bring-up with trace:
   - `tpchs noisy`, V0 and V1
2. Full primary suite with trace:
   - 4 workloads x 2 variants
3. If stable, repeat once with `TRACE=0` for cleaner timing

## One-command wrapper
Use:

```bash
bash scripts/run_g0_phase1_ablation.sh
```

This wrapper archives each variant's outputs under `runs_g0_phase1/` to avoid CSV overwrite.
