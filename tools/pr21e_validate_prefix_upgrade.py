#!/usr/bin/env python3
"""PR21e offline validation runner for the PR21b prefix-upgrade lane.

The runner consumes existing PR20c/PR20d/PR20e/PR20f CSV artifacts and produces
offline/shadow validation reports. It does not connect to a database, run
queries, create/drop indexes, or change any AdaSelectPP runtime behavior.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


OUTPUT_ROOT = Path("runs_pr21e_offline_validation")
SCRIPT_PATH = Path("tools/pr21e_validate_prefix_upgrade.py")

DEFAULT_PREFIX = ("movie_info", ("mi_movie_id",))
DEFAULT_COMPOSITE = ("movie_info", ("mi_movie_id", "mi_info_type_id"))
DEFAULT_NEAR_MARGIN_WINDOWS = (0.01, 0.02, 0.03, 0.05)
DEFAULT_SINGLE_QUERY_DOMINANCE_THRESHOLD = 0.5
FLOAT_FORMAT_POLICY = ".12g"
STABLE_SORTING_POLICY = "primary_status, source_artifact, numeric round_id, numeric gate_threshold, row_index"

STATUS_OPERATOR_INELIGIBLE = "operator_ineligible"
STATUS_ONLINE_REJECT = "online_reject_nonpositive_whatif"
STATUS_SHADOW_DEFER = "shadow_defer_positive_whatif"
PRIMARY_STATUS_ORDER = {
    STATUS_OPERATOR_INELIGIBLE: 0,
    STATUS_ONLINE_REJECT: 1,
    STATUS_SHADOW_DEFER: 2,
}

NOT_COMPUTABLE_MISSING_ARTIFACT = "NOT_COMPUTABLE_MISSING_ARTIFACT"
NOT_COMPUTABLE_MISSING_COLUMN = "NOT_COMPUTABLE_MISSING_COLUMN"
NOT_COMPUTABLE_NO_GROUND_TRUTH_LABEL = "NOT_COMPUTABLE_NO_GROUND_TRUTH_LABEL"
NOT_COMPUTABLE_NO_SHARED_JOIN_KEY = "NOT_COMPUTABLE_NO_SHARED_JOIN_KEY"
SELF_CHECK_PASSED = "SELF_CHECK_PASSED"
SELF_CHECK_FAILED = "SELF_CHECK_FAILED"

DIAG_NEAR_MARGIN = "near_margin"
DIAG_SIGN_UNSTABLE = "sign_unstable"
DIAG_CONFLICTING_EVIDENCE = "conflicting_real_or_shadow_evidence"
DIAG_SINGLE_QUERY_DOMINATED = "single_query_dominated"
DIAG_MISSING_STORAGE = "missing_storage_or_maintenance_evidence"
DIAG_POSITIVE_REAL_SUPPORT = "positive_whatif_with_real_support_observed"
DIAG_MISSING_COLUMN = "not_computable_missing_column"
DIAG_NO_GROUND_TRUTH = "not_computable_no_ground_truth_label"
DIAG_NO_SHARED_JOIN_KEY = "not_computable_no_shared_join_key"

ROUND_OUTPUT_COLUMNS = [
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
    "gate_outcome",
    "query_level_concentration",
    "top_query_delta_share",
    "storage_evidence_status",
    "write_maintenance_evidence_status",
    "transition_cost_evidence_status",
]

SUMMARY_COLUMNS = ["section", "metric", "value", "status", "notes"]


PR20C_CANDIDATE_COLUMNS = [
    "benchmark",
    "workload_type",
    "round_id",
    "width2_index",
    "table",
    "columns",
    "baseline_config",
    "baseline_cost",
    "add_config",
    "add_cost",
    "add_delta",
    "add_relative_improvement",
    "add_feasible",
    "add_infeasible_reason",
    "swap_prefix_index",
    "swap_config",
    "swap_cost",
    "swap_delta",
    "swap_relative_improvement",
    "swap_feasible",
    "swap_infeasible_reason",
    "best_mode",
    "oracle_pass_add",
    "oracle_pass_swap",
]

PR20C_ROUND_COLUMNS = [
    "round_id",
    "num_width2_candidates_tested",
    "num_add_feasible",
    "num_swap_feasible",
    "best_add_delta",
    "best_swap_delta",
    "best_add_relative_improvement",
    "best_swap_relative_improvement",
    "add_oracle_win",
    "swap_oracle_win",
    "best_add_index",
    "best_swap_index",
]

PR20C_SUMMARY_COLUMNS = [
    "rounds",
    "tested_width2_candidates",
    "add_win_rounds",
    "swap_win_rounds",
    "mean_best_add_relative_improvement",
    "mean_best_swap_relative_improvement",
    "max_best_add_relative_improvement",
    "max_best_swap_relative_improvement",
    "conclusion",
]

PR20D_QUERY_COLUMNS = [
    "round_id",
    "query_id",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "exec_delta_ms",
    "exec_relative_improvement",
    "plan_uses_prefix_index",
    "plan_uses_composite_index",
    "notes",
]

PR20D_ROUND_COLUMNS = [
    "round_id",
    "round_role",
    "baseline_config",
    "swap_config",
    "prefix_index",
    "composite_index",
    "pr20c_swap_relative_improvement",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "exec_delta_ms",
    "exec_relative_improvement",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "num_queries",
    "prefix_plan_used_query_count",
    "composite_plan_used_query_count",
    "positive_query_count",
    "top_query_delta_ms",
    "top_query_delta_share",
    "notes",
]

PR20D_SUMMARY_COLUMNS = [
    "rounds",
    "winning_rounds_tested",
    "control_rounds_tested",
    "improved_rounds_at_threshold",
    "flat_or_worse_rounds",
    "mean_exec_relative_improvement",
    "median_exec_relative_improvement",
    "max_exec_relative_improvement",
    "prefix_plan_used_query_count",
    "composite_plan_used_query_count",
    "mean_top_query_delta_share",
    "conclusion",
]

PR20E_ROUND_COLUMNS = [
    "round_id",
    "sample_category",
    "unstable_excluded",
    "unstable_reason",
    "pr20c_whatif_rel_improvement",
    "baseline_config",
    "swap_config",
    "prefix_index",
    "composite_index",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "baseline_exec_ms_mean",
    "swap_exec_ms_mean",
    "baseline_exec_ms_stdev",
    "swap_exec_ms_stdev",
    "baseline_cv",
    "swap_cv",
    "run_order_id",
    "run_order",
    "real_exec_delta_ms",
    "real_exec_rel_improvement",
    "outcome",
    "plan_uses_prefix_count",
    "plan_uses_composite_count",
    "query_level_concentration",
    "num_queries",
    "notes",
]

PR20E_QUERY_COLUMNS = [
    "round_id",
    "sample_category",
    "query_id",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "exec_delta_ms",
    "exec_rel_improvement",
    "plan_uses_prefix_index",
    "plan_uses_composite_index",
    "notes",
]

PR20E_SUMMARY_COLUMNS = [
    "row_type",
    "sample_category",
    "round_count",
    "mean_real_exec_rel_improvement",
    "median_real_exec_rel_improvement",
    "min_real_exec_rel_improvement",
    "max_real_exec_rel_improvement",
    "improved_count",
    "worse_count",
    "flat_count",
    "excluded_round_count",
    "excluded_round_ids",
    "excluded_reason_counts",
    "baseline_cv_summary",
    "swap_cv_summary",
    "spearman_rank_correlation",
    "sign_agreement_rate",
    "ordering_diagnostic_label",
    "notes",
]

PR20E_EXCLUDED_COLUMNS = [
    "round_id",
    "original_sample_category",
    "unstable_reason",
    "baseline_cv",
    "swap_cv",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "pr20c_whatif_rel_improvement",
    "notes",
]

PR20F_ROUND_COLUMNS = [
    "round_id",
    "sample_category",
    "unstable_excluded",
    "unstable_reason",
    "old_config",
    "swap_config",
    "prefix_index",
    "composite_index",
    "target_swap_whatif_rel_improvement",
    "best_swap_index",
    "best_swap_whatif_rel_improvement",
    "is_target_best",
    "gate_threshold",
    "gate_accept",
    "gate_reject",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "baseline_exec_ms_mean",
    "swap_exec_ms_mean",
    "baseline_exec_ms_stdev",
    "swap_exec_ms_stdev",
    "baseline_cv",
    "swap_cv",
    "run_order_id",
    "run_order",
    "real_exec_delta_ms",
    "real_exec_rel_improvement",
    "real_outcome",
    "gate_outcome",
    "plan_uses_prefix_count",
    "plan_uses_composite_count",
    "query_level_concentration",
    "num_queries",
    "prefix_index_size_bytes",
    "composite_index_size_bytes",
    "storage_delta_bytes",
    "storage_delta_ratio",
    "notes",
]

PR20F_QUERY_COLUMNS = [
    "round_id",
    "sample_category",
    "query_id",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "exec_delta_ms",
    "exec_rel_improvement",
    "plan_uses_prefix_index",
    "plan_uses_composite_index",
    "notes",
]

PR20F_GATE_METRICS_COLUMNS = [
    "threshold",
    "tested_count",
    "accept_count",
    "reject_count",
    "true_accept_count",
    "false_accept_count",
    "true_reject_count",
    "false_reject_count",
    "accept_precision",
    "reject_success_rate",
    "false_accept_rate",
    "false_reject_rate",
]

PR20F_SUMMARY_COLUMNS = [
    "row_type",
    "sample_category",
    "round_count",
    "mean_real_exec_rel_improvement",
    "median_real_exec_rel_improvement",
    "min_real_exec_rel_improvement",
    "max_real_exec_rel_improvement",
    "improved_count",
    "worse_count",
    "flat_count",
    "excluded_round_count",
    "excluded_round_ids",
    "notes",
]

PR20F_EXCLUDED_COLUMNS = [
    "round_id",
    "original_sample_category",
    "unstable_reason",
    "baseline_cv",
    "swap_cv",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "target_swap_whatif_rel_improvement",
    "notes",
]


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    path: Path
    expected_columns: Tuple[str, ...]


@dataclass
class ArtifactAudit:
    spec: ArtifactSpec
    exists: bool
    row_count: int
    content_hash: str
    actual_columns: List[str]
    missing_columns: List[str]
    dtype_notes: List[str]
    rows: List[Dict[str, str]]


def fmt_float(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return format(value, FLOAT_FORMAT_POLICY)
    return str(value)


def parse_float(value: object) -> Optional[float]:
    text = str(value if value is not None else "").strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: object) -> Optional[int]:
    text = str(value if value is not None else "").strip()
    if text == "":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def boolish(value: object) -> bool:
    return str(value if value is not None else "").strip().lower() in {"1", "1.0", "true", "yes"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_csv_artifact(spec: ArtifactSpec) -> ArtifactAudit:
    if not spec.path.exists():
        return ArtifactAudit(
            spec=spec,
            exists=False,
            row_count=0,
            content_hash=NOT_COMPUTABLE_MISSING_ARTIFACT,
            actual_columns=[],
            missing_columns=list(spec.expected_columns),
            dtype_notes=[],
            rows=[],
        )

    with spec.path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [dict(row) for row in reader]
        actual_columns = list(reader.fieldnames or [])

    missing_columns = [col for col in spec.expected_columns if col not in actual_columns]
    return ArtifactAudit(
        spec=spec,
        exists=True,
        row_count=len(rows),
        content_hash=sha256_file(spec.path),
        actual_columns=actual_columns,
        missing_columns=missing_columns,
        dtype_notes=infer_dtype_notes(rows, actual_columns),
        rows=rows,
    )


def infer_dtype_notes(rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> List[str]:
    notes: List[str] = []
    for col in columns:
        values = [str(row.get(col, "")).strip() for row in rows if str(row.get(col, "")).strip() != ""]
        if not values:
            notes.append(f"{col}:empty")
            continue
        numeric_count = sum(1 for value in values if parse_float(value) is not None)
        numeric_like = numeric_count == len(values)
        notes.append(f"{col}:non_empty={len(values)},numeric_like={str(numeric_like).lower()}")
    return notes


def require_columns(audit: ArtifactAudit, columns: Sequence[str]) -> Tuple[bool, List[str]]:
    if not audit.exists:
        return False, list(columns)
    missing = [col for col in columns if col not in audit.actual_columns]
    return not missing, missing


def normalize_name(value: object) -> str:
    return str(value if value is not None else "").strip().strip("'\"").lower()


def normalize_index(value: object) -> Optional[Tuple[str, Tuple[str, ...]]]:
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        table = normalize_name(value[0])
        raw_cols = value[1]
        if isinstance(raw_cols, str):
            cols = (normalize_name(raw_cols),)
        elif isinstance(raw_cols, (tuple, list)):
            cols = tuple(normalize_name(col) for col in raw_cols)
        else:
            cols = tuple(normalize_name(col) for col in value[1:])
        cols = tuple(col for col in cols if col)
        if table and cols:
            return table, cols
        return None

    if isinstance(value, str):
        text = value.strip()
        if "(" in text and text.endswith(")"):
            table, rest = text.split("(", 1)
            cols_text = rest[:-1]
            cols = tuple(normalize_name(part) for part in cols_text.split(",") if normalize_name(part))
            table_norm = normalize_name(table)
            if table_norm and cols:
                return table_norm, cols
        try:
            parsed = ast.literal_eval(text)
        except Exception:
            return None
        return normalize_index(parsed)

    return None


def format_index(index: Tuple[str, Tuple[str, ...]]) -> str:
    return f"{index[0]}({','.join(index[1])})"


def parse_config(value: object) -> Optional[Tuple[Tuple[str, Tuple[str, ...]], ...]]:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if ";" in text:
        indexes = []
        for part in text.split(";"):
            if not part.strip():
                continue
            index = normalize_index(part)
            if index is None:
                return None
            indexes.append(index)
        if indexes:
            return tuple(sorted(set(indexes)))
    try:
        parsed = ast.literal_eval(text)
    except Exception:
        index = normalize_index(text)
        if index is None:
            return None
        return (index,)
    if not isinstance(parsed, (list, tuple)):
        return None
    indexes = []
    for item in parsed:
        index = normalize_index(item)
        if index is None:
            return None
        indexes.append(index)
    return tuple(sorted(set(indexes)))


def verify_prefix_upgrade_operator(
    baseline_config_text: object,
    swap_config_text: object,
    prefix_index: Tuple[str, Tuple[str, ...]] = DEFAULT_PREFIX,
    composite_index: Tuple[str, Tuple[str, ...]] = DEFAULT_COMPOSITE,
) -> Tuple[str, str]:
    baseline = parse_config(baseline_config_text)
    swap = parse_config(swap_config_text)
    if baseline is None or swap is None:
        return STATUS_OPERATOR_INELIGIBLE, "config_parse_failed"
    baseline_set = set(baseline)
    swap_set = set(swap)
    if prefix_index not in baseline_set:
        return STATUS_OPERATOR_INELIGIBLE, "prefix_missing_from_baseline"
    if composite_index in baseline_set:
        return STATUS_OPERATOR_INELIGIBLE, "composite_already_in_baseline"
    if prefix_index in swap_set:
        return STATUS_OPERATOR_INELIGIBLE, "prefix_still_in_swap_config"
    if composite_index not in swap_set:
        return STATUS_OPERATOR_INELIGIBLE, "composite_missing_from_swap_config"
    if baseline_set - {prefix_index} != swap_set - {composite_index}:
        return STATUS_OPERATOR_INELIGIBLE, "non_atomic_prefix_upgrade_shape"
    return "operator_eligible", "exact_prefix_to_composite_upgrade"


def verify_pr20c_operator(row: Mapping[str, str]) -> Tuple[str, str]:
    prefix = normalize_index(row.get("swap_prefix_index", ""))
    width2 = normalize_index(row.get("width2_index", ""))
    if prefix == DEFAULT_PREFIX and width2 == DEFAULT_COMPOSITE:
        return "operator_eligible", "target_prefix_and_composite_match"
    return STATUS_OPERATOR_INELIGIBLE, "not_dominant_target_prefix_upgrade"


def classify_primary_status(operator_status: str, whatif_gain: Optional[float]) -> str:
    if operator_status != "operator_eligible":
        return STATUS_OPERATOR_INELIGIBLE
    if whatif_gain is None:
        return STATUS_OPERATOR_INELIGIBLE
    if whatif_gain <= 0:
        return STATUS_ONLINE_REJECT
    return STATUS_SHADOW_DEFER


def real_label_from_row(row: Mapping[str, str], preferred_fields: Sequence[str]) -> Tuple[str, str]:
    for field in preferred_fields:
        value = str(row.get(field, "")).strip()
        if value:
            return field, value
    return "", ""


def is_positive_real_label(label: str) -> bool:
    return label.strip().lower() == "improved"


def is_nonpositive_real_label(label: str) -> bool:
    return label.strip().lower() in {"flat", "worse"}


def near_margin_windows(value: Optional[float], windows: Sequence[float]) -> List[float]:
    if value is None:
        return []
    return [window for window in windows if abs(value) <= window]


def collect_diagnostic_flags(
    *,
    whatif_gain: Optional[float],
    real_label: str,
    sample_category: str,
    near_windows: Sequence[float],
    query_level_concentration: Optional[float],
    top_query_delta_share: Optional[float],
    storage_evidence_missing: bool,
    ground_truth_missing: bool,
    missing_column: bool,
    no_shared_join_key: bool,
    single_query_dominance_threshold: float = DEFAULT_SINGLE_QUERY_DOMINANCE_THRESHOLD,
) -> List[str]:
    flags = set()
    sample_category_norm = sample_category.strip().lower()

    if near_windows or sample_category_norm == "near_margin":
        flags.add(DIAG_NEAR_MARGIN)
    if near_windows or sample_category_norm == "near_margin":
        flags.add(DIAG_SIGN_UNSTABLE)
    if whatif_gain is not None and whatif_gain > 0 and is_positive_real_label(real_label):
        flags.add(DIAG_POSITIVE_REAL_SUPPORT)
    if whatif_gain is not None:
        if whatif_gain <= 0 and is_positive_real_label(real_label):
            flags.add(DIAG_CONFLICTING_EVIDENCE)
        elif whatif_gain > 0 and is_nonpositive_real_label(real_label):
            flags.add(DIAG_CONFLICTING_EVIDENCE)

    concentration_values = [value for value in [query_level_concentration, top_query_delta_share] if value is not None]
    if concentration_values and max(concentration_values) >= single_query_dominance_threshold:
        flags.add(DIAG_SINGLE_QUERY_DOMINATED)

    if storage_evidence_missing:
        flags.add(DIAG_MISSING_STORAGE)
    if ground_truth_missing:
        flags.add(DIAG_NO_GROUND_TRUTH)
    if missing_column:
        flags.add(DIAG_MISSING_COLUMN)
    if no_shared_join_key:
        flags.add(DIAG_NO_SHARED_JOIN_KEY)

    return sorted(flags)


def build_artifact_specs(args: argparse.Namespace) -> List[ArtifactSpec]:
    pr20c = Path(args.pr20c_dir)
    pr20d = Path(args.pr20d_dir)
    pr20e = Path(args.pr20e_dir)
    pr20f = Path(args.pr20f_dir)
    return [
        ArtifactSpec("pr20c_candidates", pr20c / "pr20c_width2_oracle_candidates.csv", tuple(PR20C_CANDIDATE_COLUMNS)),
        ArtifactSpec("pr20c_rounds", pr20c / "pr20c_width2_oracle_rounds.csv", tuple(PR20C_ROUND_COLUMNS)),
        ArtifactSpec("pr20c_summary", pr20c / "pr20c_width2_oracle_summary.csv", tuple(PR20C_SUMMARY_COLUMNS)),
        ArtifactSpec("pr20d_queries", pr20d / "pr20d_real_exec_queries.csv", tuple(PR20D_QUERY_COLUMNS)),
        ArtifactSpec("pr20d_rounds", pr20d / "pr20d_real_exec_rounds.csv", tuple(PR20D_ROUND_COLUMNS)),
        ArtifactSpec("pr20d_summary", pr20d / "pr20d_real_exec_summary.csv", tuple(PR20D_SUMMARY_COLUMNS)),
        ArtifactSpec("pr20e_queries", pr20e / "pr20e_broader_replay_queries.csv", tuple(PR20E_QUERY_COLUMNS)),
        ArtifactSpec("pr20e_rounds", pr20e / "pr20e_broader_replay_rounds.csv", tuple(PR20E_ROUND_COLUMNS)),
        ArtifactSpec("pr20e_summary", pr20e / "pr20e_broader_replay_summary.csv", tuple(PR20E_SUMMARY_COLUMNS)),
        ArtifactSpec("pr20e_excluded_unstable", pr20e / "pr20e_broader_replay_excluded_unstable.csv", tuple(PR20E_EXCLUDED_COLUMNS)),
        ArtifactSpec("pr20f_queries", pr20f / "pr20f_negative_control_queries.csv", tuple(PR20F_QUERY_COLUMNS)),
        ArtifactSpec("pr20f_rounds", pr20f / "pr20f_negative_control_rounds.csv", tuple(PR20F_ROUND_COLUMNS)),
        ArtifactSpec("pr20f_gate_metrics", pr20f / "pr20f_negative_control_gate_metrics.csv", tuple(PR20F_GATE_METRICS_COLUMNS)),
        ArtifactSpec("pr20f_summary", pr20f / "pr20f_negative_control_summary.csv", tuple(PR20F_SUMMARY_COLUMNS)),
        ArtifactSpec("pr20f_excluded_unstable", pr20f / "pr20f_negative_control_excluded_unstable.csv", tuple(PR20F_EXCLUDED_COLUMNS)),
    ]


def audit_artifacts(specs: Sequence[ArtifactSpec]) -> Dict[str, ArtifactAudit]:
    return {spec.name: read_csv_artifact(spec) for spec in specs}


def storage_evidence_missing(pr20f_rounds: ArtifactAudit) -> bool:
    required = [
        "prefix_index_size_bytes",
        "composite_index_size_bytes",
        "storage_delta_bytes",
        "storage_delta_ratio",
    ]
    ok, _missing = require_columns(pr20f_rounds, required)
    if not ok:
        return True
    for row in pr20f_rounds.rows:
        if all(str(row.get(col, "")).strip() for col in required):
            return False
    return True


def no_shared_join_key_status() -> Tuple[str, str]:
    return (
        NOT_COMPUTABLE_NO_SHARED_JOIN_KEY,
        "No reliable cross-artifact join key is asserted for PR20e positive-arm recall and PR20f rejection-arm safety.",
    )


def make_round_output_row(
    *,
    source_artifact: str,
    row_index: int,
    row: Mapping[str, str],
    operator_status: str,
    operator_notes: str,
    whatif_field: str,
    whatif_value: Optional[float],
    real_label_field: str,
    real_label: str,
    storage_missing: bool,
    near_windows: Sequence[float],
    no_shared_join_key: bool,
    missing_column: bool,
    single_query_dominance_threshold: float,
) -> Dict[str, str]:
    sample_category = str(row.get("sample_category", row.get("round_role", ""))).strip()
    query_level_concentration = parse_float(row.get("query_level_concentration", ""))
    top_query_delta_share = parse_float(row.get("top_query_delta_share", ""))
    primary_status = classify_primary_status(operator_status, whatif_value)
    ground_truth_missing = real_label == ""
    flags = collect_diagnostic_flags(
        whatif_gain=whatif_value,
        real_label=real_label,
        sample_category=sample_category,
        near_windows=near_windows,
        query_level_concentration=query_level_concentration,
        top_query_delta_share=top_query_delta_share,
        storage_evidence_missing=storage_missing,
        ground_truth_missing=ground_truth_missing,
        missing_column=missing_column,
        no_shared_join_key=no_shared_join_key,
        single_query_dominance_threshold=single_query_dominance_threshold,
    )
    return {
        "source_artifact": source_artifact,
        "row_index": str(row_index),
        "round_id": str(row.get("round_id", "")).strip(),
        "sample_category": sample_category,
        "gate_threshold": str(row.get("gate_threshold", "")).strip(),
        "operator_check_status": operator_status,
        "operator_check_notes": operator_notes,
        "whatif_gain_proxy_field": whatif_field,
        "whatif_gain_proxy_value": fmt_float(whatif_value),
        "primary_status": primary_status,
        "diagnostic_flags": "|".join(flags),
        "near_margin_windows": "|".join(fmt_float(window) for window in near_windows),
        "real_evidence_label_field": real_label_field,
        "real_evidence_label": real_label,
        "gate_outcome": str(row.get("gate_outcome", "")).strip(),
        "query_level_concentration": fmt_float(query_level_concentration),
        "top_query_delta_share": fmt_float(top_query_delta_share),
        "storage_evidence_status": "BLOCKER_MISSING_STORAGE_DELTA" if storage_missing else "AVAILABLE",
        "write_maintenance_evidence_status": "BLOCKER_MISSING_WRITE_MAINTENANCE_DELTA",
        "transition_cost_evidence_status": "BLOCKER_MISSING_TRANSITION_COST",
    }


def build_by_round_rows(
    audits: Mapping[str, ArtifactAudit],
    near_margin_sweep: Sequence[float],
    single_query_dominance_threshold: float = DEFAULT_SINGLE_QUERY_DOMINANCE_THRESHOLD,
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    storage_missing = storage_evidence_missing(audits["pr20f_rounds"])
    no_shared_join_key = True

    source_specs = [
        ("pr20c_candidates", "swap_relative_improvement", ("oracle_pass_swap",), verify_pr20c_operator),
        ("pr20d_rounds", "pr20c_swap_relative_improvement", (), None),
        ("pr20e_rounds", "pr20c_whatif_rel_improvement", ("outcome",), None),
        ("pr20f_rounds", "target_swap_whatif_rel_improvement", ("real_outcome",), None),
    ]

    for artifact_name, whatif_field, label_fields, operator_func in source_specs:
        audit = audits[artifact_name]
        required_columns = ["round_id", whatif_field]
        if artifact_name == "pr20c_candidates":
            required_columns.extend(["swap_prefix_index", "width2_index"])
        elif artifact_name == "pr20d_rounds":
            required_columns.extend(["baseline_config", "swap_config"])
        elif artifact_name == "pr20e_rounds":
            required_columns.extend(["baseline_config", "swap_config"])
        elif artifact_name == "pr20f_rounds":
            required_columns.extend(["old_config", "swap_config"])
        ok, missing = require_columns(audit, required_columns)
        missing_column = not ok
        if not audit.exists:
            continue
        for idx, row in enumerate(audit.rows):
            if missing_column:
                operator_status, operator_notes = STATUS_OPERATOR_INELIGIBLE, f"missing_columns={','.join(missing)}"
                whatif = None
            else:
                if artifact_name == "pr20c_candidates":
                    operator_status, operator_notes = verify_pr20c_operator(row)
                elif artifact_name == "pr20f_rounds":
                    operator_status, operator_notes = verify_prefix_upgrade_operator(row.get("old_config", ""), row.get("swap_config", ""))
                else:
                    operator_status, operator_notes = verify_prefix_upgrade_operator(row.get("baseline_config", ""), row.get("swap_config", ""))
                whatif = parse_float(row.get(whatif_field, ""))
            real_label_field, real_label = real_label_from_row(row, label_fields)
            windows = near_margin_windows(whatif, near_margin_sweep)
            rows.append(make_round_output_row(
                source_artifact=artifact_name,
                row_index=idx,
                row=row,
                operator_status=operator_status,
                operator_notes=operator_notes,
                whatif_field=whatif_field,
                whatif_value=whatif,
                real_label_field=real_label_field,
                real_label=real_label,
                storage_missing=storage_missing,
                near_windows=windows,
                no_shared_join_key=no_shared_join_key,
                missing_column=missing_column,
                single_query_dominance_threshold=single_query_dominance_threshold,
            ))

    return sorted(rows, key=round_output_sort_key)


def round_output_sort_key(row: Mapping[str, str]) -> Tuple[int, str, int, float, int]:
    return (
        PRIMARY_STATUS_ORDER.get(row.get("primary_status", ""), 99),
        row.get("source_artifact", ""),
        parse_int(row.get("round_id", "")) if parse_int(row.get("round_id", "")) is not None else 10**9,
        parse_float(row.get("gate_threshold", "")) if parse_float(row.get("gate_threshold", "")) is not None else math.inf,
        parse_int(row.get("row_index", "")) if parse_int(row.get("row_index", "")) is not None else 10**9,
    )


def recompute_gate_metrics(round_rows: Sequence[Mapping[str, str]]) -> List[Dict[str, object]]:
    rows_by_threshold: Dict[float, List[Mapping[str, str]]] = defaultdict(list)
    for row in round_rows:
        if boolish(row.get("unstable_excluded", "")):
            continue
        threshold = parse_float(row.get("gate_threshold", ""))
        if threshold is None:
            continue
        rows_by_threshold[threshold].append(row)

    metric_rows: List[Dict[str, object]] = []
    for threshold in sorted(rows_by_threshold):
        rows = rows_by_threshold[threshold]
        counts = Counter(str(row.get("gate_outcome", "")).strip() for row in rows)
        true_accept = counts["true_accept"]
        false_accept = counts["false_accept"]
        true_reject = counts["true_reject"]
        false_reject = counts["false_reject"]
        accept = true_accept + false_accept
        reject = true_reject + false_reject
        tested = accept + reject
        metric_rows.append({
            "threshold": threshold,
            "tested_count": tested,
            "accept_count": accept,
            "reject_count": reject,
            "true_accept_count": true_accept,
            "false_accept_count": false_accept,
            "true_reject_count": true_reject,
            "false_reject_count": false_reject,
            "accept_precision": true_accept / accept if accept else None,
            "reject_success_rate": true_reject / reject if reject else None,
            "false_accept_rate": false_accept / accept if accept else None,
            "false_reject_rate": false_reject / reject if reject else None,
        })
    return metric_rows


def compare_gate_metric_rows(
    recomputed: Sequence[Mapping[str, object]],
    historical: Sequence[Mapping[str, str]],
) -> Tuple[str, List[Dict[str, str]]]:
    historical_by_threshold = {
        parse_float(row.get("threshold", "")): row
        for row in historical
        if parse_float(row.get("threshold", "")) is not None
    }
    diffs: List[Dict[str, str]] = []
    for row in recomputed:
        threshold = parse_float(row.get("threshold"))
        hist = historical_by_threshold.get(threshold)
        if hist is None:
            diffs.append({
                "threshold": fmt_float(threshold),
                "column": "__row__",
                "recomputed": "present",
                "historical": "missing",
            })
            continue
        for column in PR20F_GATE_METRICS_COLUMNS:
            rec_value = row.get(column)
            hist_text = hist.get(column, "")
            if column.endswith("_count") or column in {"tested_count", "accept_count", "reject_count"}:
                rec_int = parse_int(rec_value)
                hist_int = parse_int(hist_text)
                if rec_int != hist_int:
                    diffs.append({
                        "threshold": fmt_float(threshold),
                        "column": column,
                        "recomputed": str(rec_int),
                        "historical": str(hist_int),
                    })
            else:
                rec_float = parse_float(rec_value)
                hist_float = parse_float(hist_text)
                if rec_float is None and hist_float is None:
                    continue
                if rec_float is None or hist_float is None or abs(rec_float - hist_float) > 1e-12:
                    diffs.append({
                        "threshold": fmt_float(threshold),
                        "column": column,
                        "recomputed": fmt_float(rec_float),
                        "historical": fmt_float(hist_float),
                    })
    for threshold, hist in historical_by_threshold.items():
        if all(parse_float(row.get("threshold")) != threshold for row in recomputed):
            diffs.append({
                "threshold": fmt_float(threshold),
                "column": "__row__",
                "recomputed": "missing",
                "historical": "present",
            })
    return (SELF_CHECK_FAILED if diffs else SELF_CHECK_PASSED), diffs


def pr20f_gate_self_check(audits: Mapping[str, ArtifactAudit]) -> Tuple[str, List[Dict[str, object]], List[Dict[str, str]], str]:
    rounds = audits["pr20f_rounds"]
    metrics = audits["pr20f_gate_metrics"]
    if not rounds.exists or not metrics.exists:
        return NOT_COMPUTABLE_MISSING_ARTIFACT, [], [], "PR20f gate self-check requires round and gate-metrics artifacts."
    ok_rounds, missing_rounds = require_columns(rounds, ["gate_threshold", "gate_outcome", "unstable_excluded"])
    ok_metrics, missing_metrics = require_columns(metrics, PR20F_GATE_METRICS_COLUMNS)
    if not ok_rounds or not ok_metrics:
        missing = sorted(set(missing_rounds + missing_metrics))
        return NOT_COMPUTABLE_MISSING_COLUMN, [], [], f"Missing columns: {','.join(missing)}"
    recomputed = recompute_gate_metrics(rounds.rows)
    status, diffs = compare_gate_metric_rows(recomputed, metrics.rows)
    notes = "Recomputed Gate A metrics match historical PR20f gate metrics." if status == SELF_CHECK_PASSED else "Recomputed Gate A metrics differ from historical PR20f gate metrics."
    return status, recomputed, diffs, notes


def summarize_by_round(rows: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    summary_rows: List[Dict[str, str]] = []
    primary_counts = Counter(row["primary_status"] for row in rows)
    for status in sorted(primary_counts, key=lambda item: PRIMARY_STATUS_ORDER.get(item, 99)):
        summary_rows.append(summary_row("primary_status", status, primary_counts[status], "OK", "count of proposal rows"))

    flag_counts: Counter[str] = Counter()
    grouped_flag_counts: Dict[Tuple[str, str], int] = Counter()
    for row in rows:
        flags = [flag for flag in row.get("diagnostic_flags", "").split("|") if flag]
        for flag in flags:
            flag_counts[flag] += 1
            grouped_flag_counts[(row["primary_status"], flag)] += 1
    for flag in sorted(flag_counts):
        summary_rows.append(summary_row("diagnostic_flag", flag, flag_counts[flag], "OK", "all primary statuses"))
    for (status, flag), count in sorted(grouped_flag_counts.items(), key=lambda item: (PRIMARY_STATUS_ORDER.get(item[0][0], 99), item[0][1])):
        summary_rows.append(summary_row("diagnostic_flag_by_primary_status", f"{status}:{flag}", count, "OK", "grouped by primary_status first"))
    return summary_rows


def positive_arm_recall_summary(audits: Mapping[str, ArtifactAudit], by_round_rows: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    audit = audits["pr20e_rounds"]
    ok, missing = require_columns(audit, ["round_id", "outcome", "pr20c_whatif_rel_improvement", "baseline_config", "swap_config"])
    if not audit.exists:
        return [summary_row("positive_arm_recall", "status", "", NOT_COMPUTABLE_MISSING_ARTIFACT, str(audit.spec.path))]
    if not ok:
        return [summary_row("positive_arm_recall", "status", "", NOT_COMPUTABLE_MISSING_COLUMN, ",".join(missing))]

    by_source_round = {
        (row["source_artifact"], row["round_id"], row["row_index"]): row
        for row in by_round_rows
        if row["source_artifact"] == "pr20e_rounds"
    }
    known_positive = 0
    visible = 0
    hard_rejected = 0
    operator_ineligible = 0
    for idx, row in enumerate(audit.rows):
        if is_positive_real_label(str(row.get("outcome", ""))):
            known_positive += 1
            out = by_source_round.get(("pr20e_rounds", str(row.get("round_id", "")).strip(), str(idx)))
            if out is None:
                continue
            if out["primary_status"] == STATUS_SHADOW_DEFER:
                visible += 1
            elif out["primary_status"] == STATUS_ONLINE_REJECT:
                hard_rejected += 1
            elif out["primary_status"] == STATUS_OPERATOR_INELIGIBLE:
                operator_ineligible += 1
    return [
        summary_row("positive_arm_recall", "known_positive_arm_cases", known_positive, "OK", "PR20e rows with existing outcome=improved"),
        summary_row("positive_arm_recall", "remain_visible_not_hard_rejected", visible, "OK", "primary_status=shadow_defer_positive_whatif"),
        summary_row("positive_arm_recall", "hard_rejected", hard_rejected, "OK", "primary_status=online_reject_nonpositive_whatif"),
        summary_row("positive_arm_recall", "operator_ineligible", operator_ineligible, "OK", "operator check failed"),
    ]


def nonpositive_whatif_summary(by_round_rows: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    rows = [row for row in by_round_rows if row["primary_status"] == STATUS_ONLINE_REJECT]
    counts = Counter(row["source_artifact"] for row in rows)
    out = [summary_row("nonpositive_whatif", "total_online_reject_nonpositive_whatif", len(rows), "OK", "whatif proxy <= 0 and operator eligible")]
    for source in sorted(counts):
        out.append(summary_row("nonpositive_whatif", source, counts[source], "OK", "source-specific online reject count"))
    return out


def near_margin_summary(by_round_rows: Sequence[Mapping[str, str]], windows: Sequence[float]) -> List[Dict[str, str]]:
    out = [
        summary_row(
            "near_margin_sweep",
            "policy",
            "descriptive-only; no threshold is recommended",
            "OK",
            "reported for each configured window",
        )
    ]
    for window in windows:
        count = sum(1 for row in by_round_rows if fmt_float(window) in row.get("near_margin_windows", "").split("|"))
        out.append(summary_row("near_margin_sweep", f"window_{fmt_float(window)}", count, "OK", "abs(proxy) <= window"))
    sign_unstable_count = sum(1 for row in by_round_rows if DIAG_SIGN_UNSTABLE in row.get("diagnostic_flags", "").split("|"))
    out.append(summary_row("sign_instability", "flagged_rows", sign_unstable_count, "OK", "descriptive-only sweep-derived flag"))
    return out


def query_concentration_summary(by_round_rows: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    available = 0
    flagged = 0
    for row in by_round_rows:
        if row.get("query_level_concentration") or row.get("top_query_delta_share"):
            available += 1
        if DIAG_SINGLE_QUERY_DOMINATED in row.get("diagnostic_flags", "").split("|"):
            flagged += 1
    return [
        summary_row("query_concentration", "rows_with_existing_concentration_field", available, "OK", "uses query_level_concentration and top_query_delta_share only"),
        summary_row("query_concentration", "single_query_dominated_flagged_rows", flagged, "OK", "descriptive diagnostic flag"),
    ]


def storage_blocker_summary(audits: Mapping[str, ArtifactAudit]) -> List[Dict[str, str]]:
    pr20f_rounds = audits["pr20f_rounds"]
    missing_storage = storage_evidence_missing(pr20f_rounds)
    storage_status = "BLOCKER" if missing_storage else "OK"
    storage_notes = "PR20f storage proxy columns are missing or empty." if missing_storage else "At least one PR20f row has populated storage proxy fields."
    return [
        summary_row("storage_write_transition", "storage_delta_evidence", storage_status, storage_status, storage_notes),
        summary_row("storage_write_transition", "write_maintenance_evidence", "BLOCKER", "BLOCKER", "write-maintenance delta is not present in PR20c/20d/20e/20f artifacts"),
        summary_row("storage_write_transition", "transition_cost_evidence", "BLOCKER", "BLOCKER", "build/drop/visibility transition cost is not present in PR20c/20d/20e/20f artifacts"),
    ]


def rejection_arm_summary(
    self_check_status: str,
    recomputed_gate_metrics: Sequence[Mapping[str, object]],
    self_check_notes: str,
) -> List[Dict[str, str]]:
    out = [summary_row("rejection_arm_safety", "pr20f_gate_metrics_self_check", self_check_status, self_check_status, self_check_notes)]
    for row in recomputed_gate_metrics:
        threshold = fmt_float(row.get("threshold"))
        out.extend([
            summary_row("rejection_arm_safety", f"threshold_{threshold}_false_accept_count", row.get("false_accept_count", ""), "OK", "Gate A reproduction from PR20f rows"),
            summary_row("rejection_arm_safety", f"threshold_{threshold}_false_reject_count", row.get("false_reject_count", ""), "OK", "Gate A reproduction from PR20f rows"),
            summary_row("rejection_arm_safety", f"threshold_{threshold}_false_accept_rate", row.get("false_accept_rate", ""), "OK", "Gate A reproduction from PR20f rows"),
            summary_row("rejection_arm_safety", f"threshold_{threshold}_false_reject_rate", row.get("false_reject_rate", ""), "OK", "Gate A reproduction from PR20f rows"),
        ])
    return out


def summary_row(section: str, metric: str, value: object, status: str, notes: str) -> Dict[str, str]:
    return {
        "section": section,
        "metric": metric,
        "value": fmt_float(value),
        "status": status,
        "notes": notes,
    }


def build_summary_rows(
    audits: Mapping[str, ArtifactAudit],
    by_round_rows: Sequence[Mapping[str, str]],
    near_margin_sweep: Sequence[float],
    self_check_status: str,
    recomputed_gate_metrics: Sequence[Mapping[str, object]],
    self_check_notes: str,
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for audit in audits.values():
        status = "OK" if audit.exists and not audit.missing_columns else NOT_COMPUTABLE_MISSING_COLUMN
        if not audit.exists:
            status = NOT_COMPUTABLE_MISSING_ARTIFACT
        rows.append(summary_row("schema_audit", audit.spec.name, audit.row_count, status, str(audit.spec.path)))
    joined_status, joined_notes = no_shared_join_key_status()
    rows.append(summary_row("joined_recall", "pr20e_to_pr20f_joined_recall", "", joined_status, joined_notes))
    rows.extend(summarize_by_round(by_round_rows))
    rows.extend(positive_arm_recall_summary(audits, by_round_rows))
    rows.extend(rejection_arm_summary(self_check_status, recomputed_gate_metrics, self_check_notes))
    rows.extend(nonpositive_whatif_summary(by_round_rows))
    rows.extend(near_margin_summary(by_round_rows, near_margin_sweep))
    rows.extend(query_concentration_summary(by_round_rows))
    rows.extend(storage_blocker_summary(audits))
    rows.append(summary_row("online_activation", "PR21b-online", "blocked", "BLOCKED", "PR21b-online remains blocked."))
    return rows


def current_git_commit() -> str:
    return run_git(["rev-parse", "HEAD"]) or "UNKNOWN"


def script_git_version(script_path: Path) -> str:
    commit = run_git(["log", "-1", "--format=%H", "--", str(script_path)])
    if not commit:
        return "UNTRACKED_OR_DIRTY"
    status = run_git(["status", "--short", "--", str(script_path)])
    if status:
        return f"{commit}+dirty"
    return commit


def run_git(args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    except Exception:
        return ""
    return proc.stdout.strip()


def output_manifest(
    audits: Mapping[str, ArtifactAudit],
    output_paths: Mapping[str, Path],
    generation_timestamp: str,
    script_path: Path,
) -> Dict[str, object]:
    script_text = script_path.read_text(encoding="utf-8") if script_path.exists() else ""
    return {
        "generation_timestamp": generation_timestamp,
        "current_git_commit": current_git_commit(),
        "script_path": str(script_path),
        "script_git_commit_or_version": script_git_version(script_path),
        "script_content_hash": sha256_text(script_text),
        "input_files": [
            {
                "name": audit.spec.name,
                "path": str(audit.spec.path),
                "exists": audit.exists,
                "row_count": audit.row_count,
                "content_hash": audit.content_hash,
            }
            for audit in audits.values()
        ],
        "output_files": {name: str(path) for name, path in output_paths.items()},
        "stable_sorting_policy": STABLE_SORTING_POLICY,
        "float_formatting_policy": FLOAT_FORMAT_POLICY,
    }


def write_csv(path: Path, rows: Sequence[Mapping[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow({field: fmt_float(row.get(field, "")) for field in fieldnames})


def render_report(
    *,
    audits: Mapping[str, ArtifactAudit],
    summary_rows: Sequence[Mapping[str, str]],
    by_round_rows: Sequence[Mapping[str, str]],
    manifest: Mapping[str, object],
    self_check_status: str,
    self_check_diffs: Sequence[Mapping[str, str]],
    recomputed_gate_metrics: Sequence[Mapping[str, object]],
    near_margin_sweep: Sequence[float],
) -> str:
    lines: List[str] = []
    lines.append("# PR21e Offline Prefix-Upgrade Validation Report")
    lines.append("")
    lines.append("PR21e is an offline validation runner only. It does not change runtime behavior, selector logic, `_choose_config()`, candidate generation, scoring, budgets, `optimizer_ratio`, materialization, cooldown, payback, overlay, beta, or DML behavior.")
    lines.append("")
    lines.append("PR21b-online remains blocked.")
    lines.append("")
    lines.append("R13 proxy limitation:")
    lines.append("  PR21b/PR21c define whatif_gain as a workload-level validation concept.")
    lines.append("  PR21e uses PR20f target_swap_whatif_rel_improvement as the closest available")
    lines.append("  target-specific proxy for the dominant movie_info swap.")
    lines.append("  This proxy limitation remains a blocker/caveat for PR21b-online.")
    lines.append("")
    lines.append("## Manifest")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(manifest, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Schema Audit")
    lines.append("")
    lines.append("| Artifact | Exists | Rows | Hash | Missing columns |")
    lines.append("| --- | --- | ---: | --- | --- |")
    for audit in audits.values():
        missing = ", ".join(audit.missing_columns) if audit.missing_columns else ""
        lines.append(f"| `{audit.spec.name}` | {str(audit.exists).lower()} | {audit.row_count} | `{audit.content_hash}` | {missing} |")
    lines.append("")
    lines.append("### Expected And Actual Columns")
    lines.append("")
    for audit in audits.values():
        lines.append(f"#### `{audit.spec.name}`")
        lines.append("")
        lines.append(f"- path: `{audit.spec.path}`")
        lines.append(f"- expected columns: `{', '.join(audit.spec.expected_columns)}`")
        lines.append(f"- actual columns: `{', '.join(audit.actual_columns)}`")
        lines.append(f"- dtype notes: `{'; '.join(audit.dtype_notes)}`")
        lines.append("")
    lines.append("## Primary Status Summary")
    lines.append("")
    lines.append("| Primary status | Count |")
    lines.append("| --- | ---: |")
    status_counts = Counter(row["primary_status"] for row in by_round_rows)
    for status in sorted(status_counts, key=lambda item: PRIMARY_STATUS_ORDER.get(item, 99)):
        lines.append(f"| `{status}` | {status_counts[status]} |")
    lines.append("")
    lines.append("Reports group by primary_status first, then diagnostic flags. No online accept label is produced.")
    lines.append("")
    lines.append("## Diagnostic Flags")
    lines.append("")
    lines.append("| Primary status | Diagnostic flag | Count |")
    lines.append("| --- | --- | ---: |")
    grouped = Counter()
    for row in by_round_rows:
        for flag in [item for item in row.get("diagnostic_flags", "").split("|") if item]:
            grouped[(row["primary_status"], flag)] += 1
    for (status, flag), count in sorted(grouped.items(), key=lambda item: (PRIMARY_STATUS_ORDER.get(item[0][0], 99), item[0][1])):
        lines.append(f"| `{status}` | `{flag}` | {count} |")
    lines.append("")
    lines.append("## Positive-Arm Recall")
    lines.append("")
    for row in summary_rows:
        if row["section"] == "positive_arm_recall":
            lines.append(f"- `{row['metric']}`: {row['value']} ({row['status']}) - {row['notes']}")
    lines.append("")
    lines.append("## Rejection-Arm Safety")
    lines.append("")
    lines.append(f"PR20f Gate A self-check: `{self_check_status}`")
    lines.append("")
    if self_check_diffs:
        lines.append("### Gate Metrics Diff")
        lines.append("")
        lines.append("| Threshold | Column | Recomputed | Historical |")
        lines.append("| ---: | --- | ---: | ---: |")
        for diff in self_check_diffs:
            lines.append(f"| {diff['threshold']} | `{diff['column']}` | {diff['recomputed']} | {diff['historical']} |")
        lines.append("")
    lines.append("| Threshold | False accept count | False reject count | False accept rate | False reject rate |")
    lines.append("| ---: | ---: | ---: | ---: | ---: |")
    for row in recomputed_gate_metrics:
        lines.append(
            f"| {fmt_float(row.get('threshold'))} | {fmt_float(row.get('false_accept_count'))} | "
            f"{fmt_float(row.get('false_reject_count'))} | {fmt_float(row.get('false_accept_rate'))} | "
            f"{fmt_float(row.get('false_reject_rate'))} |"
        )
    lines.append("")
    lines.append("This is a reproduction/self-check of PR20f Gate A, not new online evidence.")
    lines.append("")
    lines.append("## Non-Positive What-If Online-Reject Cases")
    lines.append("")
    for row in summary_rows:
        if row["section"] == "nonpositive_whatif":
            lines.append(f"- `{row['metric']}`: {row['value']} ({row['status']}) - {row['notes']}")
    lines.append("")
    lines.append("## Near-Margin And Sign-Instability Sweep")
    lines.append("")
    lines.append("Near-margin and sign-instability diagnostics are descriptive-only. They are reported for each configured window; no threshold is recommended.")
    lines.append("")
    lines.append("| Window | Flagged rows |")
    lines.append("| ---: | ---: |")
    summary_by_metric = {row["metric"]: row for row in summary_rows if row["section"] == "near_margin_sweep"}
    for window in near_margin_sweep:
        metric = f"window_{fmt_float(window)}"
        lines.append(f"| {fmt_float(window)} | {summary_by_metric.get(metric, {}).get('value', '')} |")
    lines.append("")
    lines.append("## Query-Level Concentration")
    lines.append("")
    for row in summary_rows:
        if row["section"] == "query_concentration":
            lines.append(f"- `{row['metric']}`: {row['value']} ({row['status']}) - {row['notes']}")
    lines.append("")
    lines.append("## Storage, Write-Maintenance, And Transition-Cost Blockers")
    lines.append("")
    for row in summary_rows:
        if row["section"] == "storage_write_transition":
            lines.append(f"- `{row['metric']}`: {row['value']} ({row['status']}) - {row['notes']}")
    lines.append("")
    lines.append("## Join-Key Discipline")
    lines.append("")
    joined_status, joined_notes = no_shared_join_key_status()
    lines.append(f"- Allowed within-artifact join key: `round_id` for each artifact family and its own query/round files.")
    lines.append(f"- Cross-artifact PR20e-to-PR20f joined recall: `{joined_status}` - {joined_notes}")
    lines.append("")
    lines.append("## Conclusion")
    lines.append("")
    lines.append("The current artifacts can support offline/shadow validation reporting, but they do not support PR21b-online activation. Storage, write-maintenance, transition-cost, cross-window shadow stability, and Gate B state-machine evidence remain unresolved blockers.")
    lines.append("")
    lines.append("PR21b-online remains blocked.")
    lines.append("")
    return "\n".join(lines)


def run_validation(args: argparse.Namespace) -> int:
    near_margin_sweep = tuple(float(value) for value in args.near_margin_windows)
    specs = build_artifact_specs(args)
    audits = audit_artifacts(specs)
    by_round_rows = build_by_round_rows(
        audits,
        near_margin_sweep=near_margin_sweep,
        single_query_dominance_threshold=args.single_query_dominance_threshold,
    )
    self_check_status, recomputed_gate_metrics, self_check_diffs, self_check_notes = pr20f_gate_self_check(audits)
    summary_rows = build_summary_rows(
        audits,
        by_round_rows,
        near_margin_sweep,
        self_check_status,
        recomputed_gate_metrics,
        self_check_notes,
    )

    output_dir = Path(args.output_dir)
    output_paths = {
        "summary_csv": output_dir / "pr21e_validation_summary.csv",
        "by_round_csv": output_dir / "pr21e_validation_by_round.csv",
        "report_md": output_dir / "pr21e_validation_report.md",
    }
    generation_timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = output_manifest(audits, output_paths, generation_timestamp, SCRIPT_PATH)
    report = render_report(
        audits=audits,
        summary_rows=summary_rows,
        by_round_rows=by_round_rows,
        manifest=manifest,
        self_check_status=self_check_status,
        self_check_diffs=self_check_diffs,
        recomputed_gate_metrics=recomputed_gate_metrics,
        near_margin_sweep=near_margin_sweep,
    )

    write_csv(output_paths["summary_csv"], summary_rows, SUMMARY_COLUMNS)
    write_csv(output_paths["by_round_csv"], by_round_rows, ROUND_OUTPUT_COLUMNS)
    output_paths["report_md"].parent.mkdir(parents=True, exist_ok=True)
    output_paths["report_md"].write_text(report, encoding="utf-8")

    print(f"Wrote {output_paths['summary_csv']}")
    print(f"Wrote {output_paths['by_round_csv']}")
    print(f"Wrote {output_paths['report_md']}")
    print(f"PR20f Gate A self-check: {self_check_status}")
    print("PR21b-online remains blocked.")
    return 1 if self_check_status == SELF_CHECK_FAILED else 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr20c-dir", default="runs_pr20c_swap_width2_oracle")
    parser.add_argument("--pr20d-dir", default="runs_pr20d_real_exec_prefix_swap")
    parser.add_argument("--pr20e-dir", default="runs_pr20e_broader_prefix_swap_replay")
    parser.add_argument("--pr20f-dir", default="runs_pr20f_negative_control_prefix_swap_replay")
    parser.add_argument("--output-dir", default=str(OUTPUT_ROOT))
    parser.add_argument(
        "--near-margin-windows",
        nargs="+",
        type=float,
        default=list(DEFAULT_NEAR_MARGIN_WINDOWS),
        help="Descriptive-only near-margin sweep windows. No threshold is recommended.",
    )
    parser.add_argument(
        "--single-query-dominance-threshold",
        type=float,
        default=DEFAULT_SINGLE_QUERY_DOMINANCE_THRESHOLD,
        help="Descriptive-only cutoff for the single_query_dominated diagnostic flag.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run_validation(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
