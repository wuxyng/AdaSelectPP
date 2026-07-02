# PR21f Offline Cost-Evidence Gap Map

PR21f does not resolve PR21b-online blockers.
PR21f maps missing cost evidence and bounded estimates.
PR21b-online remains blocked.

This runner is offline analysis only. It does not change runtime behavior, selector logic, `_choose_config()`, candidate generation, scoring, budgets, materialization, or database state.

## Manifest

```json
{
  "cost_model_name": "pr21f_cost_evidence_gap_map",
  "cost_model_parameters_constants": {
    "storage_model": {
      "default_fill_policy": "never fill missing stats with defaults",
      "formula": "MEASURED when catalog-like prefix/composite/delta byte fields are populated; ESTIMATED_MODEL only when explicit model stats are supplied; otherwise NOT_COMPUTABLE",
      "name": "explicit_storage_stats_or_catalog_fields",
      "required_stats": [
        "prefix_index_size_bytes",
        "composite_index_size_bytes",
        "storage_delta_bytes"
      ],
      "version": "v1"
    },
    "transition_model": {
      "default_fill_policy": "never create or drop indexes",
      "formula": "MEASURED from explicit transition trace rows; no real index build/drop operations",
      "name": "transition_trace_only",
      "required_stats": [
        "transition_trace_ms"
      ],
      "version": "v1"
    },
    "write_model": {
      "default_fill_policy": "never assume write frequency is zero",
      "formula": "MEASURED from explicit write trace rows; READ_ONLY_SCOPED_ESTIMATE is descriptive only and does not set write cost to zero",
      "name": "write_trace_or_read_only_scope",
      "required_stats": [
        "write_trace_events"
      ],
      "version": "v1"
    }
  },
  "cost_model_version": "v1",
  "current_git_commit": "a4e10f628496e650117b58a945966452d1f2d172",
  "float_formatting_policy": ".12g",
  "generation_timestamp": "2026-07-02T13:02:52+08:00",
  "input_files": {
    "cost_stats": {
      "content_hash": "NOT_COMPUTABLE",
      "exists": false,
      "path": "runs_pr21f_cost_evidence\\optional_cost_stats.csv",
      "row_count": 0
    },
    "pr20c_candidates": {
      "content_hash": "0ed74f7f5dc5e4027d60177c01a89b71569f2bb6e7442a681fe75d725e617691",
      "exists": true,
      "path": "runs_pr20c_swap_width2_oracle\\pr20c_width2_oracle_candidates.csv",
      "row_count": 189
    },
    "pr20d_rounds": {
      "content_hash": "365b940e0e1f4edff700d3e332c966f007b623b903c1ca5609375bb5e7d0799f",
      "exists": true,
      "path": "runs_pr20d_real_exec_prefix_swap\\pr20d_real_exec_rounds.csv",
      "row_count": 6
    },
    "pr20e_rounds": {
      "content_hash": "433f48c05258dfcba85c5def725ef5332f9327b619e6f3db03d9a89af6709fd6",
      "exists": true,
      "path": "runs_pr20e_broader_prefix_swap_replay\\pr20e_broader_replay_rounds.csv",
      "row_count": 13
    },
    "pr20f_rounds": {
      "content_hash": "93f297780dae080b97cd27562b1f36331e380452bc5e4d1edceb64efd7ed3713",
      "exists": true,
      "path": "runs_pr20f_negative_control_prefix_swap_replay\\pr20f_negative_control_rounds.csv",
      "row_count": 68
    },
    "pr21e_by_round": {
      "content_hash": "3ec345181439f574528e8060cdc3d7ff2441f8eec98351d23fcfec0ab6ca2687",
      "exists": true,
      "path": "runs_pr21e_offline_validation\\pr21e_validation_by_round.csv",
      "row_count": 276
    },
    "pr21e_report": {
      "content_hash": "aea6d83278747200c10351ac0065e7dfdc22b7af5de8592c564b69809b983c4c",
      "exists": true,
      "path": "runs_pr21e_offline_validation\\pr21e_validation_report.md",
      "row_count": 365
    },
    "pr21e_summary": {
      "content_hash": "8d0369141f97efec75457c60c2fbb43e9b68ce53cdbf99c9826c33cc06a50ba2",
      "exists": true,
      "path": "runs_pr21e_offline_validation\\pr21e_validation_summary.csv",
      "row_count": 82
    },
    "transition_trace": {
      "content_hash": "NOT_COMPUTABLE",
      "exists": false,
      "path": "runs_pr21f_cost_evidence\\optional_transition_trace.csv",
      "row_count": 0
    },
    "write_trace": {
      "content_hash": "NOT_COMPUTABLE",
      "exists": false,
      "path": "runs_pr21f_cost_evidence\\optional_write_trace.csv",
      "row_count": 0
    }
  },
  "output_files": {
    "by_pair": "runs_pr21f_cost_evidence\\pr21f_cost_evidence_by_pair.csv",
    "report": "runs_pr21f_cost_evidence\\pr21f_cost_evidence_report.md",
    "summary": "runs_pr21f_cost_evidence\\pr21f_cost_evidence_summary.csv"
  },
  "script_content_hash": "a42c0a3dd9009bc10a5e1ed7c9ded6935fa1e416c6477fd0f0b686a79cd751b6",
  "script_git_commit_or_version": "WORKTREE_CONTENT_SHA256:a42c0a3dd9009bc10a5e1ed7c9ded6935fa1e416c6477fd0f0b686a79cd751b6",
  "script_path": "tools\\pr21f_collect_prefix_upgrade_cost_evidence.py",
  "stable_sorting_policy": "pair_source_status, pair_key, prefix_index, composite_index"
}
```

## Schema And Stat Audit

| artifact | exists | rows | content hash | expected columns | actual columns | missing columns | required stat inputs | missing stat inputs |
| --- | ---: | ---: | --- | --- | --- | --- | --- | --- |
| `pr21e_by_round` | true | 276 | `3ec345181439f574528e8060cdc3d7ff2441f8eec98351d23fcfec0ab6ca2687` | `source_artifact, row_index, round_id, sample_category, gate_threshold, operator_check_status, operator_check_notes, whatif_gain_proxy_field, whatif_gain_proxy_value, primary_status, diagnostic_flags, near_margin_windows, real_evidence_label_field, real_evidence_label, oracle_metadata_field, oracle_metadata_value, gate_outcome, query_level_concentration, top_query_delta_share, storage_evidence_status, write_maintenance_evidence_status, transition_cost_evidence_status` | `source_artifact, row_index, round_id, sample_category, gate_threshold, operator_check_status, operator_check_notes, whatif_gain_proxy_field, whatif_gain_proxy_value, primary_status, diagnostic_flags, near_margin_windows, real_evidence_label_field, real_evidence_label, oracle_metadata_field, oracle_metadata_value, gate_outcome, query_level_concentration, top_query_delta_share, storage_evidence_status, write_maintenance_evidence_status, transition_cost_evidence_status` | `` | `storage_evidence_status, write_maintenance_evidence_status, transition_cost_evidence_status` | `` |
| `pr21e_summary` | true | 82 | `8d0369141f97efec75457c60c2fbb43e9b68ce53cdbf99c9826c33cc06a50ba2` | `section, metric, value, status, notes` | `section, metric, value, status, notes` | `` | `` | `` |
| `pr21e_report` | true | 365 | `aea6d83278747200c10351ac0065e7dfdc22b7af5de8592c564b69809b983c4c` | `` | `` | `` | `` | `` |
| `pr20c_candidates` | true | 189 | `0ed74f7f5dc5e4027d60177c01a89b71569f2bb6e7442a681fe75d725e617691` | `swap_prefix_index, width2_index` | `benchmark, workload_type, round_id, width2_index, table, columns, baseline_config, baseline_cost, add_config, add_cost, add_delta, add_relative_improvement, add_feasible, add_infeasible_reason, swap_prefix_index, swap_config, swap_cost, swap_delta, swap_relative_improvement, swap_feasible, swap_infeasible_reason, best_mode, oracle_pass_add, oracle_pass_swap` | `` | `` | `` |
| `pr20d_rounds` | true | 6 | `365b940e0e1f4edff700d3e332c966f007b623b903c1ca5609375bb5e7d0799f` | `prefix_index, composite_index` | `round_id, round_role, baseline_config, swap_config, prefix_index, composite_index, pr20c_swap_relative_improvement, baseline_exec_ms_median, swap_exec_ms_median, exec_delta_ms, exec_relative_improvement, baseline_exec_ms_all, swap_exec_ms_all, num_queries, prefix_plan_used_query_count, composite_plan_used_query_count, positive_query_count, top_query_delta_ms, top_query_delta_share, notes` | `` | `` | `` |
| `pr20e_rounds` | true | 13 | `433f48c05258dfcba85c5def725ef5332f9327b619e6f3db03d9a89af6709fd6` | `prefix_index, composite_index` | `round_id, sample_category, unstable_excluded, unstable_reason, pr20c_whatif_rel_improvement, baseline_config, swap_config, prefix_index, composite_index, baseline_exec_ms_all, swap_exec_ms_all, baseline_exec_ms_median, swap_exec_ms_median, baseline_exec_ms_mean, swap_exec_ms_mean, baseline_exec_ms_stdev, swap_exec_ms_stdev, baseline_cv, swap_cv, run_order_id, run_order, real_exec_delta_ms, real_exec_rel_improvement, outcome, plan_uses_prefix_count, plan_uses_composite_count, query_level_concentration, num_queries, notes` | `` | `` | `` |
| `pr20f_rounds` | true | 68 | `93f297780dae080b97cd27562b1f36331e380452bc5e4d1edceb64efd7ed3713` | `prefix_index, composite_index, prefix_index_size_bytes, composite_index_size_bytes, storage_delta_bytes, storage_delta_ratio` | `round_id, sample_category, unstable_excluded, unstable_reason, old_config, swap_config, prefix_index, composite_index, target_swap_whatif_rel_improvement, best_swap_index, best_swap_whatif_rel_improvement, is_target_best, gate_threshold, gate_accept, gate_reject, baseline_exec_ms_all, swap_exec_ms_all, baseline_exec_ms_median, swap_exec_ms_median, baseline_exec_ms_mean, swap_exec_ms_mean, baseline_exec_ms_stdev, swap_exec_ms_stdev, baseline_cv, swap_cv, run_order_id, run_order, real_exec_delta_ms, real_exec_rel_improvement, real_outcome, gate_outcome, plan_uses_prefix_count, plan_uses_composite_count, query_level_concentration, num_queries, prefix_index_size_bytes, composite_index_size_bytes, storage_delta_bytes, storage_delta_ratio, notes` | `` | `prefix_index_size_bytes, composite_index_size_bytes, storage_delta_bytes` | `prefix_index_size_bytes, composite_index_size_bytes, storage_delta_bytes` |
| `cost_stats` | false | 0 | `NOT_COMPUTABLE` | `pair_key, storage_delta_bytes_estimate, storage_model_name, storage_model_version, storage_model_assumptions, storage_model_parameters` | `` | `pair_key, storage_delta_bytes_estimate, storage_model_name, storage_model_version, storage_model_assumptions, storage_model_parameters` | `pair_key, storage_delta_bytes_estimate, storage_model_name, storage_model_version, storage_model_assumptions, storage_model_parameters` | `pair_key, storage_delta_bytes_estimate, storage_model_name, storage_model_version, storage_model_assumptions, storage_model_parameters` |
| `write_trace` | false | 0 | `NOT_COMPUTABLE` | `pair_key, write_trace_events, trace_scope` | `` | `pair_key, write_trace_events, trace_scope` | `write_trace_events` | `write_trace_events` |
| `transition_trace` | false | 0 | `NOT_COMPUTABLE` | `pair_key, transition_trace_ms, trace_scope` | `` | `pair_key, transition_trace_ms, trace_scope` | `transition_trace_ms` | `transition_trace_ms` |

## Pair-Set Map

PR21e by-round rows are the canonical pair-row source. PR20e and PR20f pair sets are reported only as mismatch diagnostics.

| metric | value |
| --- | ---: |
| `missing_pair_rows` | 33 |
| `pairs_in_pr20e` | 1 |
| `pairs_in_pr20e_not_pr21e` | 0 |
| `pairs_in_pr20f` | 1 |
| `pairs_in_pr20f_not_pr21e` | 0 |
| `pairs_in_pr21e` | 11 |
| `pairs_in_pr21e_not_pr20e` | 10 |
| `pairs_in_pr21e_not_pr20f` | 10 |

## Cost-Evidence Status Counts

### storage_delta_bytes

| status | count |
| --- | ---: |
| `NOT_COMPUTABLE` | 44 |

### write_maintenance_events

| status | count |
| --- | ---: |
| `NOT_COMPUTABLE` | 44 |

### transition_cost_ms

| status | count |
| --- | ---: |
| `NOT_COMPUTABLE` | 44 |

## PR21e Blocker Preservation

PR21e blocker status values are preserved in 44 pair rows.

## Forbidden Calculations

No net benefit, payback, ROI, benefit/cost, cost/benefit, score, or worth label fields are produced.

## Summary Rows

| section | metric | value | status | notes |
| --- | --- | ---: | --- | --- |
| `schema_stat_audit` | `pr21e_by_round` | 276 | `MEASURED` | path=runs_pr21e_offline_validation\pr21e_validation_by_round.csv |
| `schema_stat_audit` | `pr21e_summary` | 82 | `MEASURED` | path=runs_pr21e_offline_validation\pr21e_validation_summary.csv |
| `schema_stat_audit` | `pr21e_report` | 365 | `MEASURED` | path=runs_pr21e_offline_validation\pr21e_validation_report.md |
| `schema_stat_audit` | `pr20c_candidates` | 189 | `MEASURED` | path=runs_pr20c_swap_width2_oracle\pr20c_width2_oracle_candidates.csv |
| `schema_stat_audit` | `pr20d_rounds` | 6 | `MEASURED` | path=runs_pr20d_real_exec_prefix_swap\pr20d_real_exec_rounds.csv |
| `schema_stat_audit` | `pr20e_rounds` | 13 | `MEASURED` | path=runs_pr20e_broader_prefix_swap_replay\pr20e_broader_replay_rounds.csv |
| `schema_stat_audit` | `pr20f_rounds` | 68 | `NOT_COMPUTABLE` | path=runs_pr20f_negative_control_prefix_swap_replay\pr20f_negative_control_rounds.csv; missing_stat_inputs=prefix_index_size_bytes|composite_index_size_bytes|storage_delta_bytes |
| `schema_stat_audit` | `cost_stats` | 0 | `NOT_COMPUTABLE` | path=runs_pr21f_cost_evidence\optional_cost_stats.csv; missing_columns=pair_key|storage_delta_bytes_estimate|storage_model_name|storage_model_version|storage_model_assumptions|storage_model_parameters; missing_stat_inputs=pair_key|storage_delta_bytes_estimate|storage_model_name|storage_model_version|storage_model_assumptions|storage_model_parameters |
| `schema_stat_audit` | `write_trace` | 0 | `NOT_COMPUTABLE` | path=runs_pr21f_cost_evidence\optional_write_trace.csv; missing_columns=pair_key|write_trace_events|trace_scope; missing_stat_inputs=write_trace_events |
| `schema_stat_audit` | `transition_trace` | 0 | `NOT_COMPUTABLE` | path=runs_pr21f_cost_evidence\optional_transition_trace.csv; missing_columns=pair_key|transition_trace_ms|trace_scope; missing_stat_inputs=transition_trace_ms |
| `pair_set` | `missing_pair_rows` | 33 | `MEASURED` | reported separately from canonical pair rows |
| `pair_set` | `pairs_in_pr21e` | 11 | `MEASURED` | reported separately from canonical pair rows |
| `pair_set` | `pairs_in_pr20e` | 1 | `MEASURED` | reported separately from canonical pair rows |
| `pair_set` | `pairs_in_pr20f` | 1 | `MEASURED` | reported separately from canonical pair rows |
| `pair_set` | `pairs_in_pr20e_not_pr21e` | 0 | `MEASURED` | reported separately from canonical pair rows |
| `pair_set` | `pairs_in_pr20f_not_pr21e` | 0 | `MEASURED` | reported separately from canonical pair rows |
| `pair_set` | `pairs_in_pr21e_not_pr20e` | 10 | `MEASURED` | reported separately from canonical pair rows |
| `pair_set` | `pairs_in_pr21e_not_pr20f` | 10 | `MEASURED` | reported separately from canonical pair rows |
| `storage_delta_bytes` | `NOT_COMPUTABLE` | 44 | `NOT_COMPUTABLE` | cost-evidence status count |
| `write_maintenance_events` | `NOT_COMPUTABLE` | 44 | `NOT_COMPUTABLE` | cost-evidence status count |
| `transition_cost_ms` | `NOT_COMPUTABLE` | 44 | `NOT_COMPUTABLE` | cost-evidence status count |
| `pr21e_blocker_preservation` | `pairs_with_preserved_pr21e_blocker` | 44 | `MEASURED` | PR21e status values are copied into PR21f output rows |
| `forbidden_calculations` | `forbidden_fields_absent` | true | `MEASURED` | no net benefit, payback, ROI, ratio, score, or worth label fields |
| `online_activation` | `PR21b-online` | blocked | `NOT_COMPUTABLE` | PR21b-online remains blocked. |

## Conclusion

PR21f does not resolve PR21b-online blockers.
PR21f maps missing cost evidence and bounded estimates.
PR21b-online remains blocked.
