# PR19 Candidate-Pool Validation Results

PR19 validates candidate-pool quality, not online policy. The experiment keeps
the downstream selector fixed as `offline_pool_celf` and compares exported
candidate pools from:

- `candidate_generation_mode=probe_grow`
- `candidate_generation_mode=probe_grow_fair`

The main result is that `probe_grow_fair` keeps pool size unchanged while
reallocating the pool toward width-2 candidates.

## Summary

| case | rounds | fair_win_rate | width2_delta_mean | selected_width2_delta_mean | improvement_delta_mean | selected_overlap_jaccard_mean |
|---|---:|---:|---:|---:|---:|---:|
| `tpch_random` | 11 | 0.727 | 0.727 | 0.727 | 0.000067 | 0.875 |
| `tpchs_random` | 11 | 0.545 | 0.727 | 0.636 | 0.010723 | 0.884 |
| `tpchs_noisy` | 10 | 0.400 | 0.500 | 0.400 | 0.005834 | 0.927 |
| `tpchs_shifting` | 8 | 0.000 | 0.000 | 0.000 | 0.000000 | 1.000 |

## Interpretation

The strongest evidence is `tpchs_random`: `width2_delta_mean=0.727`,
`selected_width2_delta_mean=0.636`, and `improvement_delta_mean=0.010723`.
This shows that fair candidate generation exposes more width-2 candidates and
that the fixed offline selector can use them for better validated cost.

`tpchs_noisy` shows sparse positive gains: 4 positive rounds, 6 zero rounds, no
negative rounds, and `improvement_delta_mean=0.005834`. This is consistent with
fair mode acting only when useful supply pressure exists, rather than
introducing broad churn.

`tpch_random` shows selected width-2 improvement but near-zero performance delta.
The baseline relative improvement is already near saturation, so additional
width-2 selection has little room to improve the offline objective.

`tpchs_shifting` is neutral because `probe_grow` and `probe_grow_fair` exported
identical candidate pools in all 8 rounds. Fair mode was enabled, but it did not
rescue or add candidates because no round-cap starvation event occurred.

## Limitations

PR19 is an offline/export/analyzer validation harness. It does not validate
online selector policy, materialization, replacement overlay behavior, beta
gating, cooldown, payback, or online feedback dynamics.

The offline selector is `offline_pool_celf`, a pool-restricted deterministic
oracle. It does not import or reproduce LiteSelect two-CELF/RPA, and it does not
call AdaSelectPP candidate generation internally.

`num_templates_covered` is not used for PR19 conclusions until exporter
instrumentation is verified. Current conclusions are based on pool size,
width-2 supply, selected width-2 count, relative improvement, and selected-set
overlap.
