# PR21g-1 Offline Storage And Transition Measurement

storage size was measured in isolated DB;
isolated create/drop transition timing was measured;
write-maintenance and online contention remain unmeasured blockers;
PR21b-online remains blocked.

Size API used: `pg_relation_size` for index relation main-size measurement.

## Manifest

```json
{
  "concurrent_load_observed": "unknown",
  "cpu_os": "Linux 5.4.0-150-generic x86_64",
  "current_git_commit": "c6596bca0bed2421cdfc1df9307770ea61639c85",
  "database_name": "job",
  "dataset_scale_note": "unknown",
  "environment_fingerprint": "00a980392fe8f27e69e796de62d36432edaa5cee9422af4df67b35797603088f",
  "float_formatting_policy": ".12g",
  "generation_timestamp": "2026-07-03T13:54:03+08:00",
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
    "hash": "d3ce3a07896c84e89ba3fc3c6934f90fb316023518c97d2b1bc67d6fa463814d",
    "missing_columns": [],
    "path": "runs_pr21e_offline_validation/pr21e_validation_by_round.csv",
    "row_count": 276
  },
  "maintenance_work_mem": "64MB",
  "max_parallel_maintenance_workers": "2",
  "movie_info_row_count": "14835720",
  "output_files": {
    "by_pair": "runs_pr21g_storage_transition/pr21g_storage_transition_by_pair.csv",
    "report": "runs_pr21g_storage_transition/pr21g_storage_transition_report.md",
    "summary": "runs_pr21g_storage_transition/pr21g_storage_transition_summary.csv"
  },
  "postgresql_version": "12.0",
  "schema_table_row_count": "14835720",
  "script_git_commit_or_version": "SCRIPT_CONTENT_SHA256:cbe75e76ff08fe0b78a737a18f3824722bc6388061664fb6afd3064417611414",
  "script_hash": "cbe75e76ff08fe0b78a737a18f3824722bc6388061664fb6afd3064417611414",
  "script_path": "tools/pr21g_measure_prefix_upgrade_storage_transition.py",
  "shared_buffers": "128MB",
  "size_api_used": "pg_relation_size",
  "stable_sorting_policy": "pair_key",
  "storage_type": "unknown",
  "timing_repetitions_n": 5,
  "work_mem": "4MB"
}
```

## Input Artifact

- path: `runs_pr21e_offline_validation/pr21e_validation_by_round.csv`
- exists: `true`
- row count: `276`
- hash: `d3ce3a07896c84e89ba3fc3c6934f90fb316023518c97d2b1bc67d6fa463814d`
- missing columns: ``

## Pair

- prefix: `movie_info(mi_movie_id)`
- composite: `movie_info(mi_movie_id,mi_info_type_id)`
- canonical source: `hardcoded_default_pair_fields_missing`
- online_contention_still_blocked: `true`

## Summary

| section | metric | value | evidence_source | measurement_scope | status | notes |
| --- | --- | ---: | --- | --- | --- | --- |
| `input` | `pr21e_by_round_rows` | 276 | `MEASURED` | `NOT_COMPUTABLE` | `MEASURED` | runs_pr21e_offline_validation/pr21e_validation_by_round.csv |
| `precondition` | `movie_info_table` | true | `MEASURED` | `NOT_COMPUTABLE` | `MEASURED` | driver=psycopg2 |
| `precondition` | `movie_info_row_count` | 14835720 | `MEASURED` | `MEASURED_CATALOG_SIZE` | `MEASURED` | nonzero table required before measuring storage |
| `storage` | `prefix_size_bytes` | 333643776 | `MEASURED` | `MEASURED_CATALOG_SIZE` | `MEASURED` | pg_relation_size |
| `storage` | `composite_size_bytes` | 333398016 | `MEASURED` | `MEASURED_CATALOG_SIZE` | `MEASURED` | pg_relation_size |
| `storage` | `storage_delta_bytes` | -245760 | `MEASURED` | `MEASURED_CATALOG_SIZE` | `MEASURED` | composite minus prefix |
| `storage` | `transient_peak_storage_bytes` | 667041792 | `MEASURED` | `MEASURED_CATALOG_SIZE` | `MEASURED` | prefix plus composite |
| `storage` | `storage_delta_ratio_vs_prefix` | -0.000736593989393 | `MEASURED` | `MEASURED_CATALOG_SIZE` | `MEASURED` | storage_delta_bytes / prefix_size_bytes |
| `transition` | `create_composite_ms_median` | 5864.36426296 | `MEASURED` | `MEASURED_ISOLATED_SINGLE_CONN` | `MEASURED` | repetitions=5 |
| `transition` | `drop_composite_ms_median` | 31.5119629959 | `MEASURED` | `MEASURED_ISOLATED_SINGLE_CONN` | `MEASURED` | repetitions=5 |
| `transition` | `create_prefix_ms_median` | 5749.10874298 | `MEASURED` | `MEASURED_ISOLATED_SINGLE_CONN` | `MEASURED` | repetitions=5 |
| `transition` | `drop_prefix_ms_median` | 30.8915680507 | `MEASURED` | `MEASURED_ISOLATED_SINGLE_CONN` | `MEASURED` | repetitions=5 |
| `scope` | `write_maintenance_measured` | false | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | PR21g-1 does not measure write-maintenance. |
| `scope` | `online_contention_still_blocked` | true | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | PR21g-1 does not measure online contention. |
| `schema` | `forbidden_fields_absent` | true | `MEASURED` | `NOT_COMPUTABLE` | `MEASURED` | forbidden decision fields are absent |
| `online_activation` | `PR21b-online` | blocked | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | `NOT_COMPUTABLE` | PR21b-online remains blocked. |

## Conclusion

storage size was measured in isolated DB;
isolated create/drop transition timing was measured;
write-maintenance and online contention remain unmeasured blockers;
PR21b-online remains blocked.
