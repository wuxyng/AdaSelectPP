# PR21e Offline Prefix-Upgrade Validation Report

PR21e is an offline validation runner only. It does not change runtime behavior, selector logic, `_choose_config()`, candidate generation, scoring, budgets, `optimizer_ratio`, materialization, cooldown, payback, overlay, beta, or DML behavior.

PR21b-online remains blocked.

R13 proxy limitation:
  PR21b/PR21c define whatif_gain as a workload-level validation concept.
  PR21e uses PR20f target_swap_whatif_rel_improvement as the closest available
  target-specific proxy for the dominant movie_info swap.
  This proxy limitation remains a blocker/caveat for PR21b-online.

## Manifest

```json
{
  "current_git_commit": "909d08ce6e7efbe42181cd102eaf0f6e1529fa65",
  "float_formatting_policy": ".12g",
  "generation_timestamp": "2026-07-01T16:26:03Z",
  "input_files": [
    {
      "content_hash": "0ed74f7f5dc5e4027d60177c01a89b71569f2bb6e7442a681fe75d725e617691",
      "exists": true,
      "name": "pr20c_candidates",
      "path": "runs_pr20c_swap_width2_oracle\\pr20c_width2_oracle_candidates.csv",
      "row_count": 189
    },
    {
      "content_hash": "f0b515d4612fbdfa48478d42a24966dd95739b44b1978bb40edf57f970775744",
      "exists": true,
      "name": "pr20c_rounds",
      "path": "runs_pr20c_swap_width2_oracle\\pr20c_width2_oracle_rounds.csv",
      "row_count": 25
    },
    {
      "content_hash": "82e35ef2e0b232156b36065df94df16401088dcb124e3fc203d38fd3ecc7f793",
      "exists": true,
      "name": "pr20c_summary",
      "path": "runs_pr20c_swap_width2_oracle\\pr20c_width2_oracle_summary.csv",
      "row_count": 1
    },
    {
      "content_hash": "a01adfd2db74dda2374fb56c99b5edfeac4d8f2ffa43ed3f1d0ff5779054bf22",
      "exists": true,
      "name": "pr20d_queries",
      "path": "runs_pr20d_real_exec_prefix_swap\\pr20d_real_exec_queries.csv",
      "row_count": 198
    },
    {
      "content_hash": "365b940e0e1f4edff700d3e332c966f007b623b903c1ca5609375bb5e7d0799f",
      "exists": true,
      "name": "pr20d_rounds",
      "path": "runs_pr20d_real_exec_prefix_swap\\pr20d_real_exec_rounds.csv",
      "row_count": 6
    },
    {
      "content_hash": "060dac88e3fc2cf1ac8fed92dec66e67779f977bf29906da27c1a1f4b05ce6cb",
      "exists": true,
      "name": "pr20d_summary",
      "path": "runs_pr20d_real_exec_prefix_swap\\pr20d_real_exec_summary.csv",
      "row_count": 1
    },
    {
      "content_hash": "199f51c949ab933e5df46dc3835407520b07311ab210b103b1960d5b4159a0ed",
      "exists": true,
      "name": "pr20e_queries",
      "path": "runs_pr20e_broader_prefix_swap_replay\\pr20e_broader_replay_queries.csv",
      "row_count": 429
    },
    {
      "content_hash": "433f48c05258dfcba85c5def725ef5332f9327b619e6f3db03d9a89af6709fd6",
      "exists": true,
      "name": "pr20e_rounds",
      "path": "runs_pr20e_broader_prefix_swap_replay\\pr20e_broader_replay_rounds.csv",
      "row_count": 13
    },
    {
      "content_hash": "2740bb6aeb6350494c256d43f0da0a31b810a0a59fc9539501b2149ba0314862",
      "exists": true,
      "name": "pr20e_summary",
      "path": "runs_pr20e_broader_prefix_swap_replay\\pr20e_broader_replay_summary.csv",
      "row_count": 6
    },
    {
      "content_hash": "38eb8565f4f1798c5c2f6d4a2df108257dbbccaad995a7684a53db7c065482dd",
      "exists": true,
      "name": "pr20e_excluded_unstable",
      "path": "runs_pr20e_broader_prefix_swap_replay\\pr20e_broader_replay_excluded_unstable.csv",
      "row_count": 0
    },
    {
      "content_hash": "7ad8bfdd7ed546a5c09847176af2d1e2397670025858219806da1a8d0e8df41e",
      "exists": true,
      "name": "pr20f_queries",
      "path": "runs_pr20f_negative_control_prefix_swap_replay\\pr20f_negative_control_queries.csv",
      "row_count": 561
    },
    {
      "content_hash": "93f297780dae080b97cd27562b1f36331e380452bc5e4d1edceb64efd7ed3713",
      "exists": true,
      "name": "pr20f_rounds",
      "path": "runs_pr20f_negative_control_prefix_swap_replay\\pr20f_negative_control_rounds.csv",
      "row_count": 68
    },
    {
      "content_hash": "12eefaaf0ad43d3c0f5c19e3922ec8136e6b91b1f050f5def500de76eca2b9bb",
      "exists": true,
      "name": "pr20f_gate_metrics",
      "path": "runs_pr20f_negative_control_prefix_swap_replay\\pr20f_negative_control_gate_metrics.csv",
      "row_count": 4
    },
    {
      "content_hash": "1d3bb1ce9585a6fb90cfac65b11ca4d590dc59c75abe638c5661b99aa6a6e031",
      "exists": true,
      "name": "pr20f_summary",
      "path": "runs_pr20f_negative_control_prefix_swap_replay\\pr20f_negative_control_summary.csv",
      "row_count": 6
    },
    {
      "content_hash": "512cafb692c05bc797cb49374226bfc0b2cc38840f0713a1fb8e3e268176d9cd",
      "exists": true,
      "name": "pr20f_excluded_unstable",
      "path": "runs_pr20f_negative_control_prefix_swap_replay\\pr20f_negative_control_excluded_unstable.csv",
      "row_count": 0
    }
  ],
  "output_files": {
    "by_round_csv": "runs_pr21e_offline_validation\\pr21e_validation_by_round.csv",
    "report_md": "runs_pr21e_offline_validation\\pr21e_validation_report.md",
    "summary_csv": "runs_pr21e_offline_validation\\pr21e_validation_summary.csv"
  },
  "script_content_hash": "dcf5b99d00852d3d9b88161e5bcc954a86dccb2b3da2ebedf492f723760bee42",
  "script_git_commit_or_version": "909d08ce6e7efbe42181cd102eaf0f6e1529fa65",
  "script_path": "tools\\pr21e_validate_prefix_upgrade.py",
  "stable_sorting_policy": "primary_status, source_artifact, numeric round_id, numeric gate_threshold, row_index"
}
```

## Schema Audit

| Artifact | Exists | Rows | Hash | Missing columns |
| --- | --- | ---: | --- | --- |
| `pr20c_candidates` | true | 189 | `0ed74f7f5dc5e4027d60177c01a89b71569f2bb6e7442a681fe75d725e617691` |  |
| `pr20c_rounds` | true | 25 | `f0b515d4612fbdfa48478d42a24966dd95739b44b1978bb40edf57f970775744` |  |
| `pr20c_summary` | true | 1 | `82e35ef2e0b232156b36065df94df16401088dcb124e3fc203d38fd3ecc7f793` |  |
| `pr20d_queries` | true | 198 | `a01adfd2db74dda2374fb56c99b5edfeac4d8f2ffa43ed3f1d0ff5779054bf22` |  |
| `pr20d_rounds` | true | 6 | `365b940e0e1f4edff700d3e332c966f007b623b903c1ca5609375bb5e7d0799f` |  |
| `pr20d_summary` | true | 1 | `060dac88e3fc2cf1ac8fed92dec66e67779f977bf29906da27c1a1f4b05ce6cb` |  |
| `pr20e_queries` | true | 429 | `199f51c949ab933e5df46dc3835407520b07311ab210b103b1960d5b4159a0ed` |  |
| `pr20e_rounds` | true | 13 | `433f48c05258dfcba85c5def725ef5332f9327b619e6f3db03d9a89af6709fd6` |  |
| `pr20e_summary` | true | 6 | `2740bb6aeb6350494c256d43f0da0a31b810a0a59fc9539501b2149ba0314862` |  |
| `pr20e_excluded_unstable` | true | 0 | `38eb8565f4f1798c5c2f6d4a2df108257dbbccaad995a7684a53db7c065482dd` |  |
| `pr20f_queries` | true | 561 | `7ad8bfdd7ed546a5c09847176af2d1e2397670025858219806da1a8d0e8df41e` |  |
| `pr20f_rounds` | true | 68 | `93f297780dae080b97cd27562b1f36331e380452bc5e4d1edceb64efd7ed3713` |  |
| `pr20f_gate_metrics` | true | 4 | `12eefaaf0ad43d3c0f5c19e3922ec8136e6b91b1f050f5def500de76eca2b9bb` |  |
| `pr20f_summary` | true | 6 | `1d3bb1ce9585a6fb90cfac65b11ca4d590dc59c75abe638c5661b99aa6a6e031` |  |
| `pr20f_excluded_unstable` | true | 0 | `512cafb692c05bc797cb49374226bfc0b2cc38840f0713a1fb8e3e268176d9cd` |  |

### Expected And Actual Columns

#### `pr20c_candidates`

- path: `runs_pr20c_swap_width2_oracle\pr20c_width2_oracle_candidates.csv`
- expected columns: `benchmark, workload_type, round_id, width2_index, table, columns, baseline_config, baseline_cost, add_config, add_cost, add_delta, add_relative_improvement, add_feasible, add_infeasible_reason, swap_prefix_index, swap_config, swap_cost, swap_delta, swap_relative_improvement, swap_feasible, swap_infeasible_reason, best_mode, oracle_pass_add, oracle_pass_swap`
- actual columns: `benchmark, workload_type, round_id, width2_index, table, columns, baseline_config, baseline_cost, add_config, add_cost, add_delta, add_relative_improvement, add_feasible, add_infeasible_reason, swap_prefix_index, swap_config, swap_cost, swap_delta, swap_relative_improvement, swap_feasible, swap_infeasible_reason, best_mode, oracle_pass_add, oracle_pass_swap`
- dtype notes: `benchmark:non_empty=189,numeric_like=false; workload_type:non_empty=189,numeric_like=false; round_id:non_empty=189,numeric_like=true; width2_index:non_empty=189,numeric_like=false; table:non_empty=189,numeric_like=false; columns:non_empty=189,numeric_like=false; baseline_config:non_empty=189,numeric_like=false; baseline_cost:non_empty=189,numeric_like=true; add_config:empty; add_cost:empty; add_delta:non_empty=189,numeric_like=true; add_relative_improvement:non_empty=189,numeric_like=true; add_feasible:non_empty=189,numeric_like=true; add_infeasible_reason:non_empty=189,numeric_like=false; swap_prefix_index:non_empty=156,numeric_like=false; swap_config:non_empty=156,numeric_like=false; swap_cost:non_empty=156,numeric_like=true; swap_delta:non_empty=189,numeric_like=true; swap_relative_improvement:non_empty=189,numeric_like=true; swap_feasible:non_empty=189,numeric_like=true; swap_infeasible_reason:non_empty=33,numeric_like=false; best_mode:non_empty=189,numeric_like=false; oracle_pass_add:non_empty=189,numeric_like=true; oracle_pass_swap:non_empty=189,numeric_like=true`

#### `pr20c_rounds`

- path: `runs_pr20c_swap_width2_oracle\pr20c_width2_oracle_rounds.csv`
- expected columns: `round_id, num_width2_candidates_tested, num_add_feasible, num_swap_feasible, best_add_delta, best_swap_delta, best_add_relative_improvement, best_swap_relative_improvement, add_oracle_win, swap_oracle_win, best_add_index, best_swap_index`
- actual columns: `round_id, num_width2_candidates_tested, num_add_feasible, num_swap_feasible, best_add_delta, best_swap_delta, best_add_relative_improvement, best_swap_relative_improvement, add_oracle_win, swap_oracle_win, best_add_index, best_swap_index`
- dtype notes: `round_id:non_empty=25,numeric_like=true; num_width2_candidates_tested:non_empty=25,numeric_like=true; num_add_feasible:non_empty=25,numeric_like=true; num_swap_feasible:non_empty=25,numeric_like=true; best_add_delta:non_empty=25,numeric_like=true; best_swap_delta:non_empty=25,numeric_like=true; best_add_relative_improvement:non_empty=25,numeric_like=true; best_swap_relative_improvement:non_empty=25,numeric_like=true; add_oracle_win:non_empty=25,numeric_like=true; swap_oracle_win:non_empty=25,numeric_like=true; best_add_index:empty; best_swap_index:non_empty=23,numeric_like=false`

#### `pr20c_summary`

- path: `runs_pr20c_swap_width2_oracle\pr20c_width2_oracle_summary.csv`
- expected columns: `rounds, tested_width2_candidates, add_win_rounds, swap_win_rounds, mean_best_add_relative_improvement, mean_best_swap_relative_improvement, max_best_add_relative_improvement, max_best_swap_relative_improvement, conclusion`
- actual columns: `rounds, tested_width2_candidates, add_win_rounds, swap_win_rounds, mean_best_add_relative_improvement, mean_best_swap_relative_improvement, max_best_add_relative_improvement, max_best_swap_relative_improvement, conclusion`
- dtype notes: `rounds:non_empty=1,numeric_like=true; tested_width2_candidates:non_empty=1,numeric_like=true; add_win_rounds:non_empty=1,numeric_like=true; swap_win_rounds:non_empty=1,numeric_like=true; mean_best_add_relative_improvement:non_empty=1,numeric_like=true; mean_best_swap_relative_improvement:non_empty=1,numeric_like=true; max_best_add_relative_improvement:non_empty=1,numeric_like=true; max_best_swap_relative_improvement:non_empty=1,numeric_like=true; conclusion:non_empty=1,numeric_like=false`

#### `pr20d_queries`

- path: `runs_pr20d_real_exec_prefix_swap\pr20d_real_exec_queries.csv`
- expected columns: `round_id, query_id, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_relative_improvement, plan_uses_prefix_index, plan_uses_composite_index, notes`
- actual columns: `round_id, query_id, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_relative_improvement, plan_uses_prefix_index, plan_uses_composite_index, notes`
- dtype notes: `round_id:non_empty=198,numeric_like=true; query_id:non_empty=198,numeric_like=true; baseline_exec_ms_median:non_empty=198,numeric_like=true; swap_exec_ms_median:non_empty=198,numeric_like=true; exec_delta_ms:non_empty=198,numeric_like=true; exec_relative_improvement:non_empty=198,numeric_like=true; plan_uses_prefix_index:non_empty=198,numeric_like=true; plan_uses_composite_index:non_empty=198,numeric_like=true; notes:empty`

#### `pr20d_rounds`

- path: `runs_pr20d_real_exec_prefix_swap\pr20d_real_exec_rounds.csv`
- expected columns: `round_id, round_role, baseline_config, swap_config, prefix_index, composite_index, pr20c_swap_relative_improvement, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_relative_improvement, baseline_exec_ms_all, swap_exec_ms_all, num_queries, prefix_plan_used_query_count, composite_plan_used_query_count, positive_query_count, top_query_delta_ms, top_query_delta_share, notes`
- actual columns: `round_id, round_role, baseline_config, swap_config, prefix_index, composite_index, pr20c_swap_relative_improvement, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_relative_improvement, baseline_exec_ms_all, swap_exec_ms_all, num_queries, prefix_plan_used_query_count, composite_plan_used_query_count, positive_query_count, top_query_delta_ms, top_query_delta_share, notes`
- dtype notes: `round_id:non_empty=6,numeric_like=true; round_role:non_empty=6,numeric_like=false; baseline_config:non_empty=6,numeric_like=false; swap_config:non_empty=6,numeric_like=false; prefix_index:non_empty=6,numeric_like=false; composite_index:non_empty=6,numeric_like=false; pr20c_swap_relative_improvement:non_empty=6,numeric_like=true; baseline_exec_ms_median:non_empty=6,numeric_like=true; swap_exec_ms_median:non_empty=6,numeric_like=true; exec_delta_ms:non_empty=6,numeric_like=true; exec_relative_improvement:non_empty=6,numeric_like=true; baseline_exec_ms_all:non_empty=6,numeric_like=false; swap_exec_ms_all:non_empty=6,numeric_like=false; num_queries:non_empty=6,numeric_like=true; prefix_plan_used_query_count:non_empty=6,numeric_like=true; composite_plan_used_query_count:non_empty=6,numeric_like=true; positive_query_count:non_empty=6,numeric_like=true; top_query_delta_ms:non_empty=6,numeric_like=true; top_query_delta_share:non_empty=6,numeric_like=true; notes:non_empty=6,numeric_like=false`

#### `pr20d_summary`

- path: `runs_pr20d_real_exec_prefix_swap\pr20d_real_exec_summary.csv`
- expected columns: `rounds, winning_rounds_tested, control_rounds_tested, improved_rounds_at_threshold, flat_or_worse_rounds, mean_exec_relative_improvement, median_exec_relative_improvement, max_exec_relative_improvement, prefix_plan_used_query_count, composite_plan_used_query_count, mean_top_query_delta_share, conclusion`
- actual columns: `rounds, winning_rounds_tested, control_rounds_tested, improved_rounds_at_threshold, flat_or_worse_rounds, mean_exec_relative_improvement, median_exec_relative_improvement, max_exec_relative_improvement, prefix_plan_used_query_count, composite_plan_used_query_count, mean_top_query_delta_share, conclusion`
- dtype notes: `rounds:non_empty=1,numeric_like=true; winning_rounds_tested:non_empty=1,numeric_like=true; control_rounds_tested:non_empty=1,numeric_like=true; improved_rounds_at_threshold:non_empty=1,numeric_like=true; flat_or_worse_rounds:non_empty=1,numeric_like=true; mean_exec_relative_improvement:non_empty=1,numeric_like=true; median_exec_relative_improvement:non_empty=1,numeric_like=true; max_exec_relative_improvement:non_empty=1,numeric_like=true; prefix_plan_used_query_count:non_empty=1,numeric_like=true; composite_plan_used_query_count:non_empty=1,numeric_like=true; mean_top_query_delta_share:non_empty=1,numeric_like=true; conclusion:non_empty=1,numeric_like=false`

#### `pr20e_queries`

- path: `runs_pr20e_broader_prefix_swap_replay\pr20e_broader_replay_queries.csv`
- expected columns: `round_id, sample_category, query_id, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_rel_improvement, plan_uses_prefix_index, plan_uses_composite_index, notes`
- actual columns: `round_id, sample_category, query_id, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_rel_improvement, plan_uses_prefix_index, plan_uses_composite_index, notes`
- dtype notes: `round_id:non_empty=429,numeric_like=true; sample_category:non_empty=429,numeric_like=false; query_id:non_empty=429,numeric_like=true; baseline_exec_ms_median:non_empty=429,numeric_like=true; swap_exec_ms_median:non_empty=429,numeric_like=true; exec_delta_ms:non_empty=429,numeric_like=true; exec_rel_improvement:non_empty=429,numeric_like=true; plan_uses_prefix_index:non_empty=429,numeric_like=true; plan_uses_composite_index:non_empty=429,numeric_like=true; notes:empty`

#### `pr20e_rounds`

- path: `runs_pr20e_broader_prefix_swap_replay\pr20e_broader_replay_rounds.csv`
- expected columns: `round_id, sample_category, unstable_excluded, unstable_reason, pr20c_whatif_rel_improvement, baseline_config, swap_config, prefix_index, composite_index, baseline_exec_ms_all, swap_exec_ms_all, baseline_exec_ms_median, swap_exec_ms_median, baseline_exec_ms_mean, swap_exec_ms_mean, baseline_exec_ms_stdev, swap_exec_ms_stdev, baseline_cv, swap_cv, run_order_id, run_order, real_exec_delta_ms, real_exec_rel_improvement, outcome, plan_uses_prefix_count, plan_uses_composite_count, query_level_concentration, num_queries, notes`
- actual columns: `round_id, sample_category, unstable_excluded, unstable_reason, pr20c_whatif_rel_improvement, baseline_config, swap_config, prefix_index, composite_index, baseline_exec_ms_all, swap_exec_ms_all, baseline_exec_ms_median, swap_exec_ms_median, baseline_exec_ms_mean, swap_exec_ms_mean, baseline_exec_ms_stdev, swap_exec_ms_stdev, baseline_cv, swap_cv, run_order_id, run_order, real_exec_delta_ms, real_exec_rel_improvement, outcome, plan_uses_prefix_count, plan_uses_composite_count, query_level_concentration, num_queries, notes`
- dtype notes: `round_id:non_empty=13,numeric_like=true; sample_category:non_empty=13,numeric_like=false; unstable_excluded:non_empty=13,numeric_like=true; unstable_reason:empty; pr20c_whatif_rel_improvement:non_empty=13,numeric_like=true; baseline_config:non_empty=13,numeric_like=false; swap_config:non_empty=13,numeric_like=false; prefix_index:non_empty=13,numeric_like=false; composite_index:non_empty=13,numeric_like=false; baseline_exec_ms_all:non_empty=13,numeric_like=false; swap_exec_ms_all:non_empty=13,numeric_like=false; baseline_exec_ms_median:non_empty=13,numeric_like=true; swap_exec_ms_median:non_empty=13,numeric_like=true; baseline_exec_ms_mean:non_empty=13,numeric_like=true; swap_exec_ms_mean:non_empty=13,numeric_like=true; baseline_exec_ms_stdev:non_empty=13,numeric_like=true; swap_exec_ms_stdev:non_empty=13,numeric_like=true; baseline_cv:non_empty=13,numeric_like=true; swap_cv:non_empty=13,numeric_like=true; run_order_id:non_empty=13,numeric_like=false; run_order:non_empty=13,numeric_like=false; real_exec_delta_ms:non_empty=13,numeric_like=true; real_exec_rel_improvement:non_empty=13,numeric_like=true; outcome:non_empty=13,numeric_like=false; plan_uses_prefix_count:non_empty=13,numeric_like=true; plan_uses_composite_count:non_empty=13,numeric_like=true; query_level_concentration:non_empty=13,numeric_like=true; num_queries:non_empty=13,numeric_like=true; notes:non_empty=13,numeric_like=false`

#### `pr20e_summary`

- path: `runs_pr20e_broader_prefix_swap_replay\pr20e_broader_replay_summary.csv`
- expected columns: `row_type, sample_category, round_count, mean_real_exec_rel_improvement, median_real_exec_rel_improvement, min_real_exec_rel_improvement, max_real_exec_rel_improvement, improved_count, worse_count, flat_count, excluded_round_count, excluded_round_ids, excluded_reason_counts, baseline_cv_summary, swap_cv_summary, spearman_rank_correlation, sign_agreement_rate, ordering_diagnostic_label, notes`
- actual columns: `row_type, sample_category, round_count, mean_real_exec_rel_improvement, median_real_exec_rel_improvement, min_real_exec_rel_improvement, max_real_exec_rel_improvement, improved_count, worse_count, flat_count, excluded_round_count, excluded_round_ids, excluded_reason_counts, baseline_cv_summary, swap_cv_summary, spearman_rank_correlation, sign_agreement_rate, ordering_diagnostic_label, notes`
- dtype notes: `row_type:non_empty=6,numeric_like=false; sample_category:non_empty=6,numeric_like=false; round_count:non_empty=5,numeric_like=true; mean_real_exec_rel_improvement:non_empty=4,numeric_like=true; median_real_exec_rel_improvement:non_empty=4,numeric_like=true; min_real_exec_rel_improvement:non_empty=4,numeric_like=true; max_real_exec_rel_improvement:non_empty=4,numeric_like=true; improved_count:non_empty=4,numeric_like=true; worse_count:non_empty=4,numeric_like=true; flat_count:non_empty=4,numeric_like=true; excluded_round_count:non_empty=1,numeric_like=true; excluded_round_ids:empty; excluded_reason_counts:non_empty=1,numeric_like=false; baseline_cv_summary:non_empty=1,numeric_like=false; swap_cv_summary:non_empty=1,numeric_like=false; spearman_rank_correlation:non_empty=1,numeric_like=true; sign_agreement_rate:non_empty=1,numeric_like=true; ordering_diagnostic_label:non_empty=1,numeric_like=false; notes:non_empty=1,numeric_like=false`

#### `pr20e_excluded_unstable`

- path: `runs_pr20e_broader_prefix_swap_replay\pr20e_broader_replay_excluded_unstable.csv`
- expected columns: `round_id, original_sample_category, unstable_reason, baseline_cv, swap_cv, baseline_exec_ms_all, swap_exec_ms_all, pr20c_whatif_rel_improvement, notes`
- actual columns: `round_id, original_sample_category, unstable_reason, baseline_cv, swap_cv, baseline_exec_ms_all, swap_exec_ms_all, pr20c_whatif_rel_improvement, notes`
- dtype notes: `round_id:empty; original_sample_category:empty; unstable_reason:empty; baseline_cv:empty; swap_cv:empty; baseline_exec_ms_all:empty; swap_exec_ms_all:empty; pr20c_whatif_rel_improvement:empty; notes:empty`

#### `pr20f_queries`

- path: `runs_pr20f_negative_control_prefix_swap_replay\pr20f_negative_control_queries.csv`
- expected columns: `round_id, sample_category, query_id, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_rel_improvement, plan_uses_prefix_index, plan_uses_composite_index, notes`
- actual columns: `round_id, sample_category, query_id, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_rel_improvement, plan_uses_prefix_index, plan_uses_composite_index, notes`
- dtype notes: `round_id:non_empty=561,numeric_like=true; sample_category:non_empty=561,numeric_like=false; query_id:non_empty=561,numeric_like=true; baseline_exec_ms_median:non_empty=561,numeric_like=true; swap_exec_ms_median:non_empty=561,numeric_like=true; exec_delta_ms:non_empty=561,numeric_like=true; exec_rel_improvement:non_empty=561,numeric_like=true; plan_uses_prefix_index:non_empty=561,numeric_like=true; plan_uses_composite_index:non_empty=561,numeric_like=true; notes:empty`

#### `pr20f_rounds`

- path: `runs_pr20f_negative_control_prefix_swap_replay\pr20f_negative_control_rounds.csv`
- expected columns: `round_id, sample_category, unstable_excluded, unstable_reason, old_config, swap_config, prefix_index, composite_index, target_swap_whatif_rel_improvement, best_swap_index, best_swap_whatif_rel_improvement, is_target_best, gate_threshold, gate_accept, gate_reject, baseline_exec_ms_all, swap_exec_ms_all, baseline_exec_ms_median, swap_exec_ms_median, baseline_exec_ms_mean, swap_exec_ms_mean, baseline_exec_ms_stdev, swap_exec_ms_stdev, baseline_cv, swap_cv, run_order_id, run_order, real_exec_delta_ms, real_exec_rel_improvement, real_outcome, gate_outcome, plan_uses_prefix_count, plan_uses_composite_count, query_level_concentration, num_queries, prefix_index_size_bytes, composite_index_size_bytes, storage_delta_bytes, storage_delta_ratio, notes`
- actual columns: `round_id, sample_category, unstable_excluded, unstable_reason, old_config, swap_config, prefix_index, composite_index, target_swap_whatif_rel_improvement, best_swap_index, best_swap_whatif_rel_improvement, is_target_best, gate_threshold, gate_accept, gate_reject, baseline_exec_ms_all, swap_exec_ms_all, baseline_exec_ms_median, swap_exec_ms_median, baseline_exec_ms_mean, swap_exec_ms_mean, baseline_exec_ms_stdev, swap_exec_ms_stdev, baseline_cv, swap_cv, run_order_id, run_order, real_exec_delta_ms, real_exec_rel_improvement, real_outcome, gate_outcome, plan_uses_prefix_count, plan_uses_composite_count, query_level_concentration, num_queries, prefix_index_size_bytes, composite_index_size_bytes, storage_delta_bytes, storage_delta_ratio, notes`
- dtype notes: `round_id:non_empty=68,numeric_like=true; sample_category:non_empty=68,numeric_like=false; unstable_excluded:non_empty=68,numeric_like=true; unstable_reason:empty; old_config:non_empty=68,numeric_like=false; swap_config:non_empty=68,numeric_like=false; prefix_index:non_empty=68,numeric_like=false; composite_index:non_empty=68,numeric_like=false; target_swap_whatif_rel_improvement:non_empty=68,numeric_like=true; best_swap_index:non_empty=68,numeric_like=false; best_swap_whatif_rel_improvement:non_empty=68,numeric_like=true; is_target_best:non_empty=68,numeric_like=true; gate_threshold:non_empty=68,numeric_like=true; gate_accept:non_empty=68,numeric_like=true; gate_reject:non_empty=68,numeric_like=true; baseline_exec_ms_all:non_empty=68,numeric_like=false; swap_exec_ms_all:non_empty=68,numeric_like=false; baseline_exec_ms_median:non_empty=68,numeric_like=true; swap_exec_ms_median:non_empty=68,numeric_like=true; baseline_exec_ms_mean:non_empty=68,numeric_like=true; swap_exec_ms_mean:non_empty=68,numeric_like=true; baseline_exec_ms_stdev:non_empty=68,numeric_like=true; swap_exec_ms_stdev:non_empty=68,numeric_like=true; baseline_cv:non_empty=68,numeric_like=true; swap_cv:non_empty=68,numeric_like=true; run_order_id:non_empty=68,numeric_like=false; run_order:non_empty=68,numeric_like=false; real_exec_delta_ms:non_empty=68,numeric_like=true; real_exec_rel_improvement:non_empty=68,numeric_like=true; real_outcome:non_empty=68,numeric_like=false; gate_outcome:non_empty=68,numeric_like=false; plan_uses_prefix_count:non_empty=68,numeric_like=true; plan_uses_composite_count:non_empty=68,numeric_like=true; query_level_concentration:non_empty=68,numeric_like=true; num_queries:non_empty=68,numeric_like=true; prefix_index_size_bytes:empty; composite_index_size_bytes:empty; storage_delta_bytes:empty; storage_delta_ratio:empty; notes:non_empty=68,numeric_like=false`

#### `pr20f_gate_metrics`

- path: `runs_pr20f_negative_control_prefix_swap_replay\pr20f_negative_control_gate_metrics.csv`
- expected columns: `threshold, tested_count, accept_count, reject_count, true_accept_count, false_accept_count, true_reject_count, false_reject_count, accept_precision, reject_success_rate, false_accept_rate, false_reject_rate`
- actual columns: `threshold, tested_count, accept_count, reject_count, true_accept_count, false_accept_count, true_reject_count, false_reject_count, accept_precision, reject_success_rate, false_accept_rate, false_reject_rate`
- dtype notes: `threshold:non_empty=4,numeric_like=true; tested_count:non_empty=4,numeric_like=true; accept_count:non_empty=4,numeric_like=true; reject_count:non_empty=4,numeric_like=true; true_accept_count:non_empty=4,numeric_like=true; false_accept_count:non_empty=4,numeric_like=true; true_reject_count:non_empty=4,numeric_like=true; false_reject_count:non_empty=4,numeric_like=true; accept_precision:non_empty=4,numeric_like=true; reject_success_rate:non_empty=4,numeric_like=true; false_accept_rate:non_empty=4,numeric_like=true; false_reject_rate:non_empty=4,numeric_like=true`

#### `pr20f_summary`

- path: `runs_pr20f_negative_control_prefix_swap_replay\pr20f_negative_control_summary.csv`
- expected columns: `row_type, sample_category, round_count, mean_real_exec_rel_improvement, median_real_exec_rel_improvement, min_real_exec_rel_improvement, max_real_exec_rel_improvement, improved_count, worse_count, flat_count, excluded_round_count, excluded_round_ids, notes`
- actual columns: `row_type, sample_category, round_count, mean_real_exec_rel_improvement, median_real_exec_rel_improvement, min_real_exec_rel_improvement, max_real_exec_rel_improvement, improved_count, worse_count, flat_count, excluded_round_count, excluded_round_ids, notes`
- dtype notes: `row_type:non_empty=6,numeric_like=false; sample_category:non_empty=6,numeric_like=false; round_count:non_empty=5,numeric_like=true; mean_real_exec_rel_improvement:non_empty=5,numeric_like=true; median_real_exec_rel_improvement:non_empty=5,numeric_like=true; min_real_exec_rel_improvement:non_empty=5,numeric_like=true; max_real_exec_rel_improvement:non_empty=5,numeric_like=true; improved_count:non_empty=5,numeric_like=true; worse_count:non_empty=5,numeric_like=true; flat_count:non_empty=5,numeric_like=true; excluded_round_count:non_empty=1,numeric_like=true; excluded_round_ids:empty; notes:non_empty=1,numeric_like=false`

#### `pr20f_excluded_unstable`

- path: `runs_pr20f_negative_control_prefix_swap_replay\pr20f_negative_control_excluded_unstable.csv`
- expected columns: `round_id, original_sample_category, unstable_reason, baseline_cv, swap_cv, baseline_exec_ms_all, swap_exec_ms_all, target_swap_whatif_rel_improvement, notes`
- actual columns: `round_id, original_sample_category, unstable_reason, baseline_cv, swap_cv, baseline_exec_ms_all, swap_exec_ms_all, target_swap_whatif_rel_improvement, notes`
- dtype notes: `round_id:empty; original_sample_category:empty; unstable_reason:empty; baseline_cv:empty; swap_cv:empty; baseline_exec_ms_all:empty; swap_exec_ms_all:empty; target_swap_whatif_rel_improvement:empty; notes:empty`

## Primary Status Summary

| Primary status | Count |
| --- | ---: |
| `operator_ineligible` | 168 |
| `online_reject_nonpositive_whatif` | 15 |
| `shadow_defer_positive_whatif` | 93 |

Reports group by primary_status first, then diagnostic flags. No online accept label is produced.

## Diagnostic Flags

| Primary status | Diagnostic flag | Count |
| --- | --- | ---: |
| `operator_ineligible` | `missing_storage_or_maintenance_evidence` | 168 |
| `operator_ineligible` | `near_margin` | 163 |
| `operator_ineligible` | `not_computable_no_shared_join_key` | 168 |
| `operator_ineligible` | `sign_unstable` | 163 |
| `online_reject_nonpositive_whatif` | `missing_storage_or_maintenance_evidence` | 15 |
| `online_reject_nonpositive_whatif` | `near_margin` | 15 |
| `online_reject_nonpositive_whatif` | `not_computable_no_shared_join_key` | 15 |
| `online_reject_nonpositive_whatif` | `sign_unstable` | 15 |
| `online_reject_nonpositive_whatif` | `single_query_dominated` | 12 |
| `shadow_defer_positive_whatif` | `conflicting_real_or_shadow_evidence` | 25 |
| `shadow_defer_positive_whatif` | `missing_storage_or_maintenance_evidence` | 93 |
| `shadow_defer_positive_whatif` | `near_margin` | 80 |
| `shadow_defer_positive_whatif` | `not_computable_no_ground_truth_label` | 6 |
| `shadow_defer_positive_whatif` | `not_computable_no_shared_join_key` | 93 |
| `shadow_defer_positive_whatif` | `positive_whatif_with_real_support_observed` | 44 |
| `shadow_defer_positive_whatif` | `sign_unstable` | 80 |
| `shadow_defer_positive_whatif` | `single_query_dominated` | 74 |

## Positive-Arm Recall

- `known_positive_arm_cases`: 12 (OK) - PR20e rows with existing outcome=improved
- `remain_visible_not_hard_rejected`: 12 (OK) - primary_status=shadow_defer_positive_whatif
- `hard_rejected`: 0 (OK) - primary_status=online_reject_nonpositive_whatif
- `operator_ineligible`: 0 (OK) - operator check failed

## Rejection-Arm Safety

PR20f Gate A self-check: `SELF_CHECK_PASSED`

| Threshold | False accept count | False reject count | False accept rate | False reject rate |
| ---: | ---: | ---: | ---: | ---: |
| 0.01 | 5 | 1 | 0.416666666667 | 0.2 |
| 0.02 | 5 | 1 | 0.416666666667 | 0.2 |
| 0.03 | 2 | 4 | 0.333333333333 | 0.363636363636 |
| 0.05 | 0 | 6 | 0 | 0.4 |

This is a reproduction/self-check of PR20f Gate A, not new online evidence.

## Non-Positive What-If Online-Reject Cases

- `total_online_reject_nonpositive_whatif`: 15 (OK) - whatif proxy <= 0 and operator eligible
- `pr20c_candidates`: 3 (OK) - source-specific online reject count
- `pr20f_rounds`: 12 (OK) - source-specific online reject count

## Near-Margin And Sign-Instability Sweep

Near-margin and sign-instability diagnostics are descriptive-only. They are reported for each configured window; no threshold is recommended.

| Window | Flagged rows |
| ---: | ---: |
| 0.01 | 129 |
| 0.02 | 157 |
| 0.03 | 210 |
| 0.05 | 258 |

## Query-Level Concentration

- `rows_with_existing_concentration_field`: 87 (OK) - uses query_level_concentration and top_query_delta_share only
- `single_query_dominated_flagged_rows`: 86 (OK) - descriptive diagnostic flag

## Storage, Write-Maintenance, And Transition-Cost Blockers

- `storage_delta_evidence`: BLOCKER (BLOCKER) - PR20f storage proxy columns are missing or empty.
- `write_maintenance_evidence`: BLOCKER (BLOCKER) - write-maintenance delta is not present in PR20c/20d/20e/20f artifacts
- `transition_cost_evidence`: BLOCKER (BLOCKER) - build/drop/visibility transition cost is not present in PR20c/20d/20e/20f artifacts

## Join-Key Discipline

- Allowed within-artifact join key: `round_id` for each artifact family and its own query/round files.
- Cross-artifact PR20e-to-PR20f joined recall: `NOT_COMPUTABLE_NO_SHARED_JOIN_KEY` - No reliable cross-artifact join key is asserted for PR20e positive-arm recall and PR20f rejection-arm safety.

## Conclusion

The current artifacts can support offline/shadow validation reporting, but they do not support PR21b-online activation. Storage, write-maintenance, transition-cost, cross-window shadow stability, and Gate B state-machine evidence remain unresolved blockers.

PR21b-online remains blocked.
