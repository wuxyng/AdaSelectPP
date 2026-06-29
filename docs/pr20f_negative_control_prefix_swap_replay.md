# PR20f Negative-Control Prefix-Swap Replay

PR20f is an offline diagnostic for the single JOB prefix-swap pattern:

```text
movie_info(mi_movie_id)
->
movie_info(mi_movie_id,mi_info_type_id)
```

It does not change AdaSelectPP online policy, `_choose_config()`, selector
logic, candidate generation, benefit/scoring logic, evaluation budget,
optimizer ratio, or materialization policy.

## Why PR20f Exists

PR20e validated positive-arm behavior only: when PR20c identified the target
swap as a target-best predicted win, real execution was usually positive.

PR20f tests the negative-control / rejection-arm behavior. Before PR21b online
activation, the proposed offline gate must show acceptable rejection behavior,
not only acceptance of good cases. The key operational risk is false accept:
allowing swaps that real execution does not improve.

Round 22 motivates a nonzero gate margin as near-margin instability evidence.
It sits close to the gate threshold and has changed outcome label across replay
runs, so PR20f treats it as a threshold warning case rather than as a fixed
numeric anecdote.

PR21b online activation remains blocked until PR20f shows acceptable rejection
behavior.

## Round Selection

PR20f selects feasible rounds where the target swap is physically meaningful:

- the executed baseline configuration for `W_t` is read from the metrics CSV
  `old` column, not `new`;
- the baseline contains `movie_info(mi_movie_id)`;
- the PR20c candidate rows contain
  `movie_info(mi_movie_id,mi_info_type_id)`;
- the swap is exactly baseline minus the prefix index plus the composite index;
- all other indexes are unchanged.

Primary negative-control categories are:

- `non_target_best_positive`: target swap has positive PR20c signal, but another
  width-2 candidate is `best_swap_index`;
- `predicted_flat_or_low`: target swap signal is positive but small;
- `predicted_negative`: target swap signal is non-positive;
- `near_margin`: target swap is close to the configured gate margin, including
  round 22 when available.

An optional `positive_anchor_optional` category can be requested for sanity
comparison, but it is kept separate from negative-control aggregates.

## Execution Cleanliness

For every selected round and physical config, PR20f requires:

- `warmup >= 1`;
- `repeats >= 3`;
- alternating baseline/swap run order;
- recorded `run_order_id` and exact `run_order`;
- median, mean, stdev, and coefficient of variation for each config;
- a configurable variance cap, default `--max-cv 0.20`.

If either config exceeds the variance cap, the round is marked
`excluded_unstable` and omitted from primary aggregates. Excluded rounds are
still written to `pr20f_negative_control_excluded_unstable.csv`.

## Physical-Index Safety

PR20f creates physical experimental indexes only when
`--experimental-physical-indexes` is passed. It uses deterministic `pr20f_`
index names, drops PR20f experimental indexes before each config materializes,
and drops them again in `finally`. It does not touch non-PR20f indexes.

Plan-use reporting checks PostgreSQL JSON plan nodes by exact `Index Name`
equality against the deterministic experimental index name. It is not substring
matching.

## Gate Simulation

PR20f simulates gate decisions offline. It does not change online code.

For each selected round and each configured threshold:

```text
gate_accept = target_swap_whatif_rel_improvement >= threshold
gate_reject = not gate_accept
```

Real outcomes use the same default threshold as PR20e:

```text
improved if real_exec_rel_improvement >= 0.01
worse    if real_exec_rel_improvement <= -0.01
flat     otherwise
```

Gate outcomes are:

- `true_accept`: accepted and real execution improved;
- `false_accept`: accepted and real execution was flat or worse;
- `true_reject`: rejected and real execution was flat or worse;
- `false_reject`: rejected and real execution improved;
- `excluded_unstable`: omitted because variance exceeded the cap.

## Storage Proxy

Composite `(c1,c2)` indexes can serve leading-column access paths for `(c1)` in
read plans. However, composite indexes are wider and may have higher storage and
write-maintenance cost.

JOB is read-mostly in this diagnostic and does not validate write-heavy
behavior. PR21b net benefit must eventually include storage and maintenance
deltas, not query runtime alone. PR20f output includes storage proxy columns,
but the current PR20f artifacts did not populate those fields. Treat storage
proxy values as TODO/unavailable until the size query path is fixed and
validated.

## Outputs

PR20f writes:

```text
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_summary.csv
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_rounds.csv
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_queries.csv
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_excluded_unstable.csv
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_gate_metrics.csv
```

Example command:

```bash
python3 tools/pr20f_negative_control_prefix_swap_replay.py \
  --benchmark job \
  --workload-type random \
  --round-size 33 \
  --database job \
  --metrics-csv runs_pr20b_job_ratio_ablation/ratio025/adaselect_job_random_a0.8_b1.5_op0.25_lamadaptive_wdcg1.csv \
  --pr20c-rounds-csv runs_pr20c_swap_width2_oracle/pr20c_width2_oracle_rounds.csv \
  --pr20c-candidates-csv runs_pr20c_swap_width2_oracle/pr20c_width2_oracle_candidates.csv \
  --output-root runs_pr20f_negative_control_prefix_swap_replay \
  --max-num 10 \
  --warmup 1 \
  --repeats 3 \
  --max-cv 0.20 \
  --gate-rel-thresholds 0.01,0.02,0.03,0.05 \
  --gate-margin-threshold 0.03 \
  --experimental-physical-indexes
```

PR20f does not implement PR21b online behavior.
