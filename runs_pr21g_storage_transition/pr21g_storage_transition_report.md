# PR21g-1 Offline Storage And Transition Measurement

PR21g-1 measures isolated storage and transition evidence only.
It does not measure write-maintenance.
It does not measure online contention.
PR21b-online remains blocked.

Size API used: `pg_relation_size` for index relation main-size measurement.

## Manifest

```json
{
  "concurrent_load_observed": "unknown",
  "cpu_os": "Windows 10 AMD64",
  "current_git_commit": "a4e10f628496e650117b58a945966452d1f2d172",
  "database_name": "unknown",
  "dataset_scale_note": "unknown",
  "environment_fingerprint": "559ba198b9995f1864dc45c1ab64c22d4179748231b49e15182b337cbc71e230",
  "float_formatting_policy": ".12g",
  "generation_timestamp": "2026-07-02T13:02:52+08:00",
  "input_artifact": {
    "columns": [
      "source_artifact",
      "row_index",
      "round_id",
      "sample_category",
      "gate_threshold",
      "operator_check_status",
      "operator_check_notes",
      "whatif_gain_proxy_field",
      "whatif_gain_proxy_value",
      "primary_status",
      "diagnostic_flags",
      "near_margin_windows",
      "real_evidence_label_field",
      "real_evidence_label",
      "oracle_metadata_field",
      "oracle_metadata_value",
      "gate_outcome",
      "query_level_concentration",
      "top_query_delta_share",
      "storage_evidence_status",
      "write_maintenance_evidence_status",
      "transition_cost_evidence_status"
    ],
    "exists": true,
    "hash": "3ec345181439f574528e8060cdc3d7ff2441f8eec98351d23fcfec0ab6ca2687",
    "missing_columns": [],
    "path": "runs_pr21e_offline_validation\\pr21e_validation_by_round.csv",
    "row_count": 276
  },
  "maintenance_work_mem": "unknown",
  "max_parallel_maintenance_workers": "unknown",
  "movie_info_row_count": "",
  "output_files": {
    "by_pair": "runs_pr21g_storage_transition\\pr21g_storage_transition_by_pair.csv",
    "report": "runs_pr21g_storage_transition\\pr21g_storage_transition_report.md",
    "summary": "runs_pr21g_storage_transition\\pr21g_storage_transition_summary.csv"
  },
  "postgresql_version": "unknown",
  "schema_table_row_count": "unknown",
  "script_git_commit_or_version": "SCRIPT_CONTENT_SHA256:14703420da3be406efe732b711470fc074efd0ccb94064d2c8efea1effbdfe4e",
  "script_hash": "14703420da3be406efe732b711470fc074efd0ccb94064d2c8efea1effbdfe4e",
  "script_path": "tools\\pr21g_measure_prefix_upgrade_storage_transition.py",
  "shared_buffers": "unknown",
  "size_api_used": "pg_relation_size",
  "stable_sorting_policy": "pair_key",
  "storage_type": "unknown",
  "timing_repetitions_n": 3,
  "work_mem": "unknown"
}
```

## Input Artifact

- path: `runs_pr21e_offline_validation\pr21e_validation_by_round.csv`
- exists: `true`
- row count: `276`
- hash: `3ec345181439f574528e8060cdc3d7ff2441f8eec98351d23fcfec0ab6ca2687`
- missing columns: ``

## Pair

- prefix: `movie_info(mi_movie_id)`
- composite: `movie_info(mi_movie_id,mi_info_type_id)`
- canonical source: `pr21e_by_round_prefix_upgrade_rows`
- online_contention_still_blocked: `true`

## Summary

| section | metric | value | evidence_source | measurement_scope | status | notes |
| --- | --- | ---: | --- | --- | --- | --- |
| `input` | `pr21e_by_round_rows` | 276 | `MEASURED` | `NOT_COMPUTABLE` | `MEASURED` | runs_pr21e_offline_validation\pr21e_validation_by_round.csv |
| `precondition` | `movie_info_table` | false | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | no --db-url was provided |
| `precondition` | `movie_info_row_count` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | nonzero table required before measuring storage |
| `storage` | `prefix_size_bytes` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | pg_relation_size |
| `storage` | `composite_size_bytes` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | pg_relation_size |
| `storage` | `storage_delta_bytes` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | composite minus prefix |
| `storage` | `transient_peak_storage_bytes` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | prefix plus composite |
| `storage` | `storage_delta_ratio_vs_prefix` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | storage_delta_bytes / prefix_size_bytes |
| `transition` | `create_composite_ms_median` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | repetitions=3 |
| `transition` | `drop_composite_ms_median` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | repetitions=3 |
| `transition` | `create_prefix_ms_median` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | repetitions=3 |
| `transition` | `drop_prefix_ms_median` |  | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE_NO_DB` | repetitions=3 |
| `scope` | `write_maintenance_measured` | false | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | PR21g-1 does not measure write-maintenance. |
| `scope` | `online_contention_still_blocked` | true | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | PR21g-1 does not measure online contention. |
| `schema` | `forbidden_fields_absent` | true | `MEASURED` | `NOT_COMPUTABLE` | `MEASURED` | forbidden decision fields are absent |
| `online_activation` | `PR21b-online` | blocked | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | PR21b-online remains blocked. |

## Conclusion

PR21g-1 measures isolated storage and transition evidence only.
It does not measure write-maintenance.
It does not measure online contention.
PR21b-online remains blocked.
