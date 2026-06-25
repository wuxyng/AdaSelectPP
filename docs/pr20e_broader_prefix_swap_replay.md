# PR20e Broader Prefix-Swap Replay

PR20e is an offline diagnostic for the single dominant JOB prefix-swap pattern:

```text
movie_info(mi_movie_id)
->
movie_info(mi_movie_id,mi_info_type_id)
```

It does not change AdaSelectPP online policy, candidate generation, selector
logic, benefit/scoring logic, evaluation budget, optimizer ratio, or
materialization policy.

## Why PR20e Exists

PR20d tested a biased high-opportunity subset. It provides existence evidence,
not distributional proof.

PR20e broadens the replay set for the same single `movie_info` prefix-swap
pattern before any PR21b selector work. It asks whether the opportunity remains
stable and positive across a broader, less selected round set.

What-if and real-exec magnitudes are not cross-unit comparable. PR20e reports
ordering agreement only as a descriptive diagnostic:

```text
DESCRIPTIVE ONLY: ordering agreement, not calibration.
```

This asks:

```text
Do the orderings agree?
```

It does not ask whether one magnitude scale predicts the other.

## Round Selection

Primary mode selects all PR20c rounds where:

```text
best_swap_index == movie_info(mi_movie_id,mi_info_type_id)
```

The selected rounds are labeled as:

- `top_win`
- `mid_win`
- `low_win`
- `control`

If the all-round replay is too expensive, stratified mode can sample top,
middle, low, and control strata explicitly.

## Execution Cleanliness

For every selected round and physical config, PR20e requires:

- `warmup >= 1`
- `repeats >= 3`
- alternating baseline/swap run order
- recorded `run_order_id` and exact `run_order`
- median, mean, stdev, and coefficient of variation for each config

The default variance cap is:

```text
--max-cv 0.20
```

Rounds exceeding the cap for either physical config are marked
`excluded_unstable` and omitted from primary aggregates. They are still written
to `pr20e_broader_replay_excluded_unstable.csv`.

## Physical-Index Safety

PR20e creates physical experimental indexes only when
`--experimental-physical-indexes` is passed. It uses deterministic `pr20e_`
index names, drops PR20e experimental indexes before materializing each config,
and drops them again in `finally`.

The baseline for replaying `W_t` is reconstructed from the metrics CSV `old`
column. The `new` column is the post-`W_t` recommendation and must not be used
to replay `W_t`.

## Outputs

PR20e writes:

```text
runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_summary.csv
runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_rounds.csv
runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_queries.csv
runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_excluded_unstable.csv
```

PR20e does not justify general selector changes by itself unless the broader
replay set is stable and positive. Selector changes belong to a later PR21b only
if PR20e supports robustness.

