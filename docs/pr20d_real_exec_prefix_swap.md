# PR20d Real Execution Prefix-Swap Validation

PR20d is an offline experimental diagnostic. It validates whether the strongest
PR20c width-2 prefix-swap pattern has real execution value, not only HypoPG
what-if value.

Target pattern:

- prefix index: `movie_info(mi_movie_id)`
- composite index: `movie_info(mi_movie_id,mi_info_type_id)`

The experiment compares, for selected `job_random` rounds:

- baseline physical config: the AdaSelectPP-selected configuration from the
  existing run artifacts,
- swap physical config: baseline minus `movie_info(mi_movie_id)` plus
  `movie_info(mi_movie_id,mi_info_type_id)`.

PR20d does not change AdaSelectPP online policy, candidate generation, eval
budget formula, optimizer ratio, selector logic, or materialization policy.

## Safety

This tool creates physical experimental indexes. It must be run only against a
scratch/local benchmark database. It refuses to run unless
`--experimental-physical-indexes` and an explicit `--database` are provided.

Index DDL is deterministic: every experimental index name is derived from the
run label, round id, config label, and index key. The tool drops its
experimental indexes before each config and after each config. It does not use
AdaSelectPP online materialization.

## Inputs

Use the same artifacts used by PR20c:

- AdaSelectPP metrics CSV for `job_random` historical `optimizer_ratio=0.25`,
- PR20c round CSV,
- PR20c candidate CSV,
- `database/workload/job_random.txt`.

The runner selects:

- top predicted-winning rounds whose PR20c `best_swap_index` is
  `movie_info(mi_movie_id,mi_info_type_id)`,
- at least one control round where the target composite was not predicted to
  pass the PR20c swap threshold, when available.

## Example Command

```bash
python3 tools/pr20d_real_exec_prefix_swap.py \
  --benchmark job \
  --workload-type random \
  --round-size 33 \
  --database job \
  --metrics-csv runs_pr20b_job_ratio_ablation/ratio025/adaselect_job_random_a0.8_b1.5_op0.25_lamadaptive_wdcg1.csv \
  --pr20c-rounds-csv runs_pr20c_swap_width2_oracle/pr20c_width2_oracle_rounds.csv \
  --pr20c-candidates-csv runs_pr20c_swap_width2_oracle/pr20c_width2_oracle_candidates.csv \
  --output-root runs_pr20d_real_exec_prefix_swap \
  --max-num 10 \
  --warmup 1 \
  --repeats 3 \
  --experimental-physical-indexes
```

## Outputs

The runner writes:

- `runs_pr20d_real_exec_prefix_swap/pr20d_real_exec_summary.csv`
- `runs_pr20d_real_exec_prefix_swap/pr20d_real_exec_rounds.csv`
- `runs_pr20d_real_exec_prefix_swap/pr20d_real_exec_queries.csv`

Round output includes median repeated execution time for baseline and swap,
the execution-time delta, relative improvement, all repeated workload timings,
plan-use counts, and a small note describing warmup/repeat settings.

Query output includes per-query medians and whether the baseline plan used the
prefix index and the swap plan used the composite index. This helps distinguish
round-wide benefit from improvement concentrated in only a few queries.

## Interpretation

If median execution relative improvement is at least `0.01` on most tested
PR20c winning rounds, PR20c what-if swap value is supported by real execution
and selector-level prefix-swap is worth pursuing.

If real execution is flat or worse while PR20c predicted large what-if gains,
PR20c identified a what-if/cost-model gap; do not implement selector changes
yet.

If only one round improves, the evidence is promising but needs seed/split
replication before PR21b.

PR20d still does not implement PR21 attribution or a retain/swap selector.

