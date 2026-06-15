#!/usr/bin/env python3
"""Summarize Phase 0.5 first-pass run CSVs.

The script intentionally uses only the Python standard library so it can run
even on a partially prepared server environment after the experiment files
exist.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


NUMERIC_COLUMNS: Sequence[str] = (
    "candidate_count_raw",
    "candidate_count",
    "evaluated_count",
    "width1_count",
    "width2_count",
    "width2_candidates_perquery_before_cap",
    "width2_candidates_perquery_after_cap",
    "width2_cap_dropped_perquery_events",
    "width2_candidates_round_before_cap",
    "width2_candidates_round_after_cap",
    "width2_cap_dropped_round",
    "pair_supply_ceiling_enabled",
    "pair_supply_ceiling_width2_added_perquery",
    "pair_supply_ceiling_width2_added_round",
    "pair_supply_ceiling_width2_survived_count",
    "pair_supply_ceiling_target_pairs_recovered",
    "pair_supply_ceiling_candidate_count_delta",
    "pair_supply_fairness_enabled",
    "pair_supply_fairness_applied_count",
    "pair_supply_fairness_rescued_width2_count",
    "pair_supply_fairness_displaced_width1_count",
    "pair_supply_fairness_columnset_dedup_count",
    "pair_supply_fairness_candidate_count_delta",
    "pair_supply_fairness_target_pairs_recovered",
    "fairness_eval_lane_enabled",
    "fairness_eval_lane_quota",
    "fairness_eval_lane_candidate_count",
    "fairness_eval_lane_evaluated_count",
    "fairness_eval_lane_replacement_diag_count",
    "fairness_eval_lane_skipped_already_evaluated_count",
    "fairness_eval_lane_budgeted_out_count",
    "fairness_eval_lane_what_if_calls",
    "fairness_eval_lane_replacement_what_if_calls",
    "fairness_eval_lane_shadowing_revealed_count",
    "fairness_eval_lane_nonbeneficial_count",
    "width1_ranked_ahead_of_best_width2",
    "best_width2_family_score",
    "max_family_score_of_displacing_width1",
    "pair_family_vs_grow_reason_mismatch",
    "seed_family_missing_count",
    "join_seed_downgraded_count",
    "seed_count",
    "eligible_seed_count",
    "multi_growth_count",
    "structural_pair_quota",
    "structural_pair_eval_count",
    "structural_pair_eval_budgeted_out_count",
    "structural_pair_eval_lane_enabled",
    "shadow_action_count",
    "shadow_add_action_count",
    "shadow_replace_action_count",
    "shadow_greedy_action_count_after_dedup",
    "shadow_duplicate_target_action_count",
    "naive_replacement_count",
    "naive_add_count",
    "naive_prefix_missing_add_count",
    "naive_pair_count",
    "stale_prefix_missing_count",
    "shadow_transition_add_count",
    "shadow_transition_drop_count",
    "shadow_transition_action_count",
    "shadow_pair_count",
    "shadow_replacement_count",
    "shadow_diff_from_active_count",
    "shadow_diff_from_candidate_count",
    "shadow_contains_lineitem_l_partkey_l_shipdate",
    "shadow_contains_orders_o_custkey_o_orderdate",
    "shadow_naive_vs_conflict_action_diff_count",
    "shadow_naive_vs_conflict_config_diff_count",
    "replacement_overlay_enabled",
    "replacement_overlay_applied_count",
    "replacement_overlay_blocked_count",
    "replacement_overlay_diff_from_topk_count",
    "overlay_opportunity_rounds",
    "overlay_lane_admitted_rounds",
    "overlay_opportunity_pair_count",
    "overlay_lane_admitted_pair_count",
    "overlay_blocked_by_lane_count",
    "overlay_blocked_by_eligibility_count",
    "overlay_fired_pair_count",
    "pair_fate_universe_count",
    "pair_fate_dropped_perquery_cap_count",
    "pair_fate_dropped_round_cap_count",
    "pair_fate_generated_not_in_overlay_opportunity_count",
    "pair_fate_in_opportunity_blocked_by_lane_count",
    "pair_fate_lane_admitted_blocked_by_eligibility_count",
    "pair_fate_lane_admitted_overlay_disabled_count",
    "pair_fate_lane_admitted_fired_count",
    "pair_fate_not_generated_other_count",
    "replacement_overlay_co_residency_count",
    "target_pair_count",
    "target_pair_prequery_coverage_count",
    "target_pair_postquery_coverage_count",
    "target_pair_preround_coverage_count",
    "target_pair_postround_coverage_count",
    "target_pair_lane_admitted_count",
    "target_pair_selected_count",
    "target_pair_final_count",
    "materialization_gap_pair_count",
    "materialization_gap_not_postround_count",
    "materialization_gap_eval_gap_count",
    "materialization_gap_prefix_shadowing_likely_count",
    "materialization_gap_replacement_positive_main_nonpositive_count",
    "materialization_gap_eval_confirmed_nonbeneficial_count",
    "materialization_gap_main_positive_but_not_selected_count",
    "materialization_gap_candidate_conf_rejected_by_beta_count",
    "materialization_gap_already_final_count",
    "materialization_gap_overlay_applied_count",
    "materialization_gap_unknown_count",
)

TOTAL_COLUMNS: Sequence[str] = (
    "what_if_calls",
    "filtered_nonpositive_count",
    "replacement_probe_count",
    "replacement_what_if_calls",
    "replacement_hit_count",
    "replacement_ok_count",
    "replacement_fail_count",
    "replacement_diag_time",
    "rejected_growth_has_or",
    "rejected_growth_alias_ambiguous",
    "rejected_growth_seed_not_positive",
    "rejected_growth_seed_unseen",
    "rejected_growth_range_seed",
    "rejected_growth_parse_fallback",
)


def _as_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return float(text)
    except Exception:
        return default


def _as_int(value: object, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


def _mean(values: Iterable[float]) -> float:
    vals = list(values)
    if not vals:
        return 0.0
    return sum(vals) / float(len(vals))


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _read_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for row in reader:
            if str(row.get("round", "")).strip().upper() == "SUMMARY":
                continue
            rows.append(row)
        return rows


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _is_metrics_csv(path: Path) -> bool:
    if path.name.endswith(".trace.csv"):
        return False
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            fields = set(reader.fieldnames or [])
        return {"round", "candidate_count", "evaluated_count"}.issubset(fields)
    except Exception:
        return False


def _is_trace_csv(path: Path) -> bool:
    if not path.name.endswith(".trace.csv"):
        return False
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            fields = set(reader.fieldnames or [])
        return {"round", "table", "cols", "in_appearing", "in_eval", "in_new"}.issubset(fields)
    except Exception:
        return False


def _under_preexisting_archive(path: Path, run_dir: Path) -> bool:
    try:
        rel = path.relative_to(run_dir)
    except ValueError:
        return False
    return "_preexisting_log_archive" in rel.parts


def _case_name(path: Path, run_dir: Path) -> str:
    try:
        rel = path.relative_to(run_dir)
    except ValueError:
        return path.stem
    if len(rel.parts) > 1:
        return rel.parts[0]
    return path.stem


def _trace_rows_for_case(csv_path: Path) -> Optional[List[Dict[str, str]]]:
    trace_paths = sorted(p for p in csv_path.parent.glob("*.trace.csv") if _is_trace_csv(p))
    if not trace_paths:
        return None
    rows: List[Dict[str, str]] = []
    for path in trace_paths:
        rows.extend(_read_csv_rows(path))
    return rows


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _width2_key(row: Dict[str, str]) -> str:
    table = str(row.get("table", "")).strip()
    cols = str(row.get("cols", "")).strip()
    return f"{table}({cols})" if table and cols else cols


def _is_width2_trace_row(row: Dict[str, str]) -> bool:
    cols = [c.strip() for c in str(row.get("cols", "")).split(",") if c.strip()]
    return len(cols) == 2


def _top_counter(counter: Counter, limit: int = 5) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{name}={count}" for name, count in counter.most_common(limit))


def _merge_table_counts(rows: List[Dict[str, str]], field: str) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        for item in str(row.get(field, "") or "").split("|"):
            if ":" not in item:
                continue
            table, value = item.split(":", 1)
            table = table.strip()
            if table:
                counter[table] += _as_int(value)
    return counter


def _example_counter(rows: List[Dict[str, str]], field: str) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        for item in str(row.get(field, "") or "").split(";"):
            item = item.strip()
            if item:
                counter[item] += 1
    return counter


def _summarize_pair_supply_metrics(rows: List[Dict[str, str]]) -> List[str]:
    fate_fields = [
        "pair_fate_dropped_perquery_cap",
        "pair_fate_dropped_round_cap",
        "pair_fate_generated_not_in_overlay_opportunity",
        "pair_fate_in_opportunity_blocked_by_lane",
        "pair_fate_lane_admitted_blocked_by_eligibility",
        "pair_fate_lane_admitted_overlay_disabled",
        "pair_fate_lane_admitted_fired",
        "pair_fate_not_generated_other",
    ]
    lines = [
        f"- width2_perquery_dropped_by_table: {_top_counter(_merge_table_counts(rows, 'width2_cap_dropped_perquery_by_table'))}",
        f"- width2_round_dropped_by_table: {_top_counter(_merge_table_counts(rows, 'width2_cap_dropped_round_by_table'))}",
        f"- width2_perquery_dropped_examples: {_top_counter(_example_counter(rows, 'width2_cap_dropped_perquery_examples'))}",
        f"- width2_round_dropped_examples: {_top_counter(_example_counter(rows, 'width2_cap_dropped_round_examples'))}",
        f"- pair_supply_ceiling_enabled_rounds: {_fmt(sum(1 for r in rows if _truthy(r.get('pair_supply_ceiling_enabled'))))}",
        f"- pair_supply_ceiling_width2_added_perquery_total: {_fmt(sum(_as_float(r.get('pair_supply_ceiling_width2_added_perquery')) for r in rows))}",
        f"- pair_supply_ceiling_width2_added_round_total: {_fmt(sum(_as_float(r.get('pair_supply_ceiling_width2_added_round')) for r in rows))}",
        f"- pair_supply_ceiling_target_pairs_recovered_total: {_fmt(sum(_as_float(r.get('pair_supply_ceiling_target_pairs_recovered')) for r in rows))}",
        f"- pair_supply_ceiling_candidate_count_delta_total: {_fmt(sum(_as_float(r.get('pair_supply_ceiling_candidate_count_delta')) for r in rows))}",
        f"- pair_supply_ceiling_examples: {_top_counter(_example_counter(rows, 'pair_supply_ceiling_examples'))}",
        f"- pair_supply_fairness_enabled_rounds: {_fmt(sum(1 for r in rows if _truthy(r.get('pair_supply_fairness_enabled'))))}",
        f"- pair_supply_fairness_applied_total: {_fmt(sum(_as_float(r.get('pair_supply_fairness_applied_count')) for r in rows))}",
        f"- pair_supply_fairness_rescued_width2_total: {_fmt(sum(_as_float(r.get('pair_supply_fairness_rescued_width2_count')) for r in rows))}",
        f"- pair_supply_fairness_displaced_width1_total: {_fmt(sum(_as_float(r.get('pair_supply_fairness_displaced_width1_count')) for r in rows))}",
        f"- pair_supply_fairness_columnset_dedup_total: {_fmt(sum(_as_float(r.get('pair_supply_fairness_columnset_dedup_count')) for r in rows))}",
        f"- pair_supply_fairness_candidate_count_delta_total: {_fmt(sum(_as_float(r.get('pair_supply_fairness_candidate_count_delta')) for r in rows))}",
        f"- pair_supply_fairness_target_pairs_recovered_total: {_fmt(sum(_as_float(r.get('pair_supply_fairness_target_pairs_recovered')) for r in rows))}",
        f"- pair_supply_fairness_rescued_pairs: {_top_counter(_example_counter(rows, 'pair_supply_fairness_rescued_pairs'))}",
        f"- pair_supply_fairness_rescued_by_table: {_top_counter(_merge_table_counts(rows, 'pair_supply_fairness_rescued_by_table'))}",
        f"- pair_supply_fairness_displaced_width1_examples: {_top_counter(_example_counter(rows, 'pair_supply_fairness_displaced_width1_keys'))}",
        f"- pair_supply_fairness_block_reasons: {_top_counter(_example_counter(rows, 'pair_supply_fairness_block_reason'))}",
        f"- pair_supply_fairness_target_pairs_recovered_examples: {_top_counter(_example_counter(rows, 'pair_supply_fairness_target_pairs_recovered_examples'))}",
        f"- fairness_eval_lane_enabled_rounds: {_fmt(sum(1 for r in rows if _truthy(r.get('fairness_eval_lane_enabled'))))}",
        f"- fairness_eval_lane_candidate_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_candidate_count')) for r in rows))}",
        f"- fairness_eval_lane_evaluated_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_evaluated_count')) for r in rows))}",
        f"- fairness_eval_lane_replacement_diag_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_replacement_diag_count')) for r in rows))}",
        f"- fairness_eval_lane_skipped_already_evaluated_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_skipped_already_evaluated_count')) for r in rows))}",
        f"- fairness_eval_lane_budgeted_out_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_budgeted_out_count')) for r in rows))}",
        f"- fairness_eval_lane_what_if_calls_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_what_if_calls')) for r in rows))}",
        f"- fairness_eval_lane_replacement_what_if_calls_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_replacement_what_if_calls')) for r in rows))}",
        f"- fairness_eval_lane_shadowing_revealed_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_shadowing_revealed_count')) for r in rows))}",
        f"- fairness_eval_lane_nonbeneficial_total: {_fmt(sum(_as_float(r.get('fairness_eval_lane_nonbeneficial_count')) for r in rows))}",
        f"- fairness_eval_lane_evaluated_pairs: {_top_counter(_example_counter(rows, 'fairness_eval_lane_evaluated_pairs'))}",
        f"- target_pair_prequery_coverage_total: {_fmt(sum(_as_float(r.get('target_pair_prequery_coverage_count')) for r in rows))}",
        f"- target_pair_postquery_coverage_total: {_fmt(sum(_as_float(r.get('target_pair_postquery_coverage_count')) for r in rows))}",
        f"- target_pair_preround_coverage_total: {_fmt(sum(_as_float(r.get('target_pair_preround_coverage_count')) for r in rows))}",
        f"- target_pair_postround_coverage_total: {_fmt(sum(_as_float(r.get('target_pair_postround_coverage_count')) for r in rows))}",
        f"- target_pair_lane_admitted_total: {_fmt(sum(_as_float(r.get('target_pair_lane_admitted_count')) for r in rows))}",
        f"- target_pair_selected_total: {_fmt(sum(_as_float(r.get('target_pair_selected_count')) for r in rows))}",
        f"- target_pair_final_total: {_fmt(sum(_as_float(r.get('target_pair_final_count')) for r in rows))}",
        f"- target_pair_missing_examples: {_top_counter(_example_counter(rows, 'target_pair_missing_examples'))}",
        f"- target_pair_dropped_perquery_examples: {_top_counter(_example_counter(rows, 'target_pair_dropped_perquery_examples'))}",
        f"- target_pair_dropped_round_examples: {_top_counter(_example_counter(rows, 'target_pair_dropped_round_examples'))}",
        f"- overlay_blocked_by_lane_pair_total: {_fmt(sum(_as_float(r.get('overlay_blocked_by_lane_count')) for r in rows))}",
        f"- overlay_blocked_by_eligibility_pair_total: {_fmt(sum(_as_float(r.get('overlay_blocked_by_eligibility_count')) for r in rows))}",
        f"- overlay_fired_pair_total: {_fmt(sum(_as_float(r.get('overlay_fired_pair_count')) for r in rows))}",
        f"- materialization_gap_pair_total: {_fmt(sum(_as_float(r.get('materialization_gap_pair_count')) for r in rows))}",
        f"- materialization_gap_not_postround_total: {_fmt(sum(_as_float(r.get('materialization_gap_not_postround_count')) for r in rows))}",
        f"- materialization_gap_eval_gap_total: {_fmt(sum(_as_float(r.get('materialization_gap_eval_gap_count')) for r in rows))}",
        f"- materialization_gap_prefix_shadowing_likely_total: {_fmt(sum(_as_float(r.get('materialization_gap_prefix_shadowing_likely_count')) for r in rows))}",
        f"- materialization_gap_replacement_positive_main_nonpositive_total: {_fmt(sum(_as_float(r.get('materialization_gap_replacement_positive_main_nonpositive_count')) for r in rows))}",
        f"- materialization_gap_eval_confirmed_nonbeneficial_total: {_fmt(sum(_as_float(r.get('materialization_gap_eval_confirmed_nonbeneficial_count')) for r in rows))}",
        f"- materialization_gap_main_positive_but_not_selected_total: {_fmt(sum(_as_float(r.get('materialization_gap_main_positive_but_not_selected_count')) for r in rows))}",
        f"- materialization_gap_candidate_conf_rejected_by_beta_total: {_fmt(sum(_as_float(r.get('materialization_gap_candidate_conf_rejected_by_beta_count')) for r in rows))}",
        f"- materialization_gap_already_final_total: {_fmt(sum(_as_float(r.get('materialization_gap_already_final_count')) for r in rows))}",
        f"- materialization_gap_overlay_applied_total: {_fmt(sum(_as_float(r.get('materialization_gap_overlay_applied_count')) for r in rows))}",
        f"- materialization_gap_unknown_total: {_fmt(sum(_as_float(r.get('materialization_gap_unknown_count')) for r in rows))}",
        f"- materialization_gap_not_postround_examples: {_top_counter(_example_counter(rows, 'materialization_gap_not_postround_examples'))}",
        f"- materialization_gap_eval_gap_examples: {_top_counter(_example_counter(rows, 'materialization_gap_eval_gap_examples'))}",
        f"- materialization_gap_prefix_shadowing_examples: {_top_counter(_example_counter(rows, 'materialization_gap_prefix_shadowing_examples'))}",
        f"- materialization_gap_replacement_positive_main_nonpositive_examples: {_top_counter(_example_counter(rows, 'materialization_gap_replacement_positive_main_nonpositive_examples'))}",
        f"- materialization_gap_eval_confirmed_nonbeneficial_examples: {_top_counter(_example_counter(rows, 'materialization_gap_eval_confirmed_nonbeneficial_examples'))}",
        f"- materialization_gap_main_positive_not_selected_examples: {_top_counter(_example_counter(rows, 'materialization_gap_main_positive_not_selected_examples'))}",
    ]
    for prefix in fate_fields:
        lines.append(f"- {prefix}_total: {_fmt(sum(_as_float(r.get(prefix + '_count')) for r in rows))}")
        examples = _example_counter(rows, prefix + "_examples")
        if examples:
            lines.append(f"- {prefix}_examples: {_top_counter(examples)}")
    return lines


def _summarize_width2_trace(trace_rows: Optional[List[Dict[str, str]]]) -> List[str]:
    if trace_rows is None:
        return ["- width2_trace: unavailable"]

    width2_rows = [r for r in trace_rows if _is_width2_trace_row(r)]
    appeared = [r for r in width2_rows if _truthy(r.get("in_appearing"))]
    evaluated = [r for r in width2_rows if _truthy(r.get("in_eval"))]
    selected = [r for r in width2_rows if _truthy(r.get("in_new"))]
    zero_benefit = [
        r for r in width2_rows
        if str(r.get("benefit", "")).strip() != "" and abs(_as_float(r.get("benefit"))) <= 1e-12
    ]
    blocked_by_budget = [r for r in appeared if not _truthy(r.get("in_eval"))]

    by_appearance = Counter(_width2_key(r) for r in appeared)
    by_evaluation = Counter(_width2_key(r) for r in evaluated)
    by_selected = Counter(_width2_key(r) for r in selected)
    by_budget = Counter(_width2_key(r) for r in blocked_by_budget)
    replacement_rows = [
        r for r in width2_rows
        if str(r.get("replacement_benefit_raw", r.get("replacement_benefit", ""))).strip() != ""
    ]
    by_replacement = Counter(
        _width2_key(r)
        for r in sorted(
            replacement_rows,
            key=lambda row: _as_float(row.get("replacement_benefit_raw", row.get("replacement_benefit"))),
            reverse=True,
        )[:5]
    )
    per_round: Dict[str, Dict[str, str]] = {}
    for row in trace_rows:
        rid = str(row.get("round", "")).strip()
        if rid and rid not in per_round:
            per_round[rid] = row
    quota_total = sum(_as_int(r.get("structural_pair_quota")) for r in per_round.values())
    structural_eval_total = sum(_as_int(r.get("structural_pair_eval_count")) for r in per_round.values())
    budgeted_total = sum(_as_int(r.get("structural_pair_eval_budgeted_out_count")) for r in per_round.values())
    lane_rounds = sum(1 for r in per_round.values() if _truthy(r.get("structural_pair_eval_lane_enabled")))

    return [
        f"- width2_appeared_count: {len(appeared)}",
        f"- width2_evaluated_count: {len(evaluated)}",
        f"- width2_selected_count: {len(selected)}",
        f"- width2_with_zero_benefit_count: {len(zero_benefit)}",
        f"- structural_pair_quota_total: {quota_total}",
        f"- structural_pair_eval_count_total: {structural_eval_total}",
        f"- structural_pair_eval_budgeted_out_count_total: {budgeted_total}",
        f"- structural_pair_eval_lane_enabled_rounds: {lane_rounds}",
        f"- top_width2_by_appearance: {_top_counter(by_appearance)}",
        f"- top_width2_by_evaluation: {_top_counter(by_evaluation)}",
        f"- top_width2_by_selected_count: {_top_counter(by_selected)}",
        f"- top_width2_blocked_by_budget: {_top_counter(by_budget)}",
        f"- top_width2_by_replacement_diagnostic: {_top_counter(by_replacement)}",
    ]


def _summarize_shadow_trace(trace_rows: Optional[List[Dict[str, str]]]) -> List[str]:
    if trace_rows is None:
        return ["- shadow_trace: unavailable"]
    action_rows = [r for r in trace_rows if str(r.get("shadow_action_key", "")).strip()]
    add_rows = [r for r in action_rows if str(r.get("shadow_action_type", "")).strip() == "ADD"]
    replace_rows = [r for r in action_rows if str(r.get("shadow_action_type", "")).strip() == "REPLACE"]

    def _top_actions(rows: List[Dict[str, str]], limit: int = 5) -> str:
        if not rows:
            return "none"
        # Deduplicate by round+action to avoid repeated trace rows when the same index is logged more than once.
        seen = set()
        deduped: List[Dict[str, str]] = []
        for row in rows:
            key = (str(row.get("round", "")), str(row.get("shadow_action_key", "")))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        top = sorted(deduped, key=lambda row: _as_float(row.get("shadow_action_utility")), reverse=True)[:limit]
        return ", ".join(
            f"{row.get('shadow_action_key')}={_fmt(_as_float(row.get('shadow_action_utility')))}"
            for row in top
        ) or "none"

    per_round: Dict[str, Dict[str, str]] = {}
    for row in trace_rows:
        rid = str(row.get("round", "")).strip()
        if rid and rid not in per_round:
            per_round[rid] = row

    return [
        f"- shadow_action_rows: {len(action_rows)}",
        f"- shadow_add_action_rows: {len(add_rows)}",
        f"- shadow_replace_action_rows: {len(replace_rows)}",
        f"- shadow_duplicate_target_action_count_total: {sum(_as_int(r.get('shadow_duplicate_target_action_count')) for r in per_round.values())}",
        f"- shadow_greedy_action_count_after_dedup_total: {sum(_as_int(r.get('shadow_greedy_action_count_after_dedup')) for r in per_round.values())}",
        f"- top_shadow_add_actions: {_top_actions(add_rows)}",
        f"- top_shadow_replace_actions: {_top_actions(replace_rows)}",
        f"- shadow_naive_vs_conflict_action_diff_total: {sum(_as_int(r.get('shadow_naive_vs_conflict_action_diff_count')) for r in per_round.values())}",
        f"- shadow_naive_vs_conflict_config_diff_total: {sum(_as_int(r.get('shadow_naive_vs_conflict_config_diff_count')) for r in per_round.values())}",
        f"- shadow_contains_lineitem_l_partkey_l_shipdate_rounds: {sum(1 for r in per_round.values() if _truthy(r.get('shadow_contains_lineitem_l_partkey_l_shipdate')))}",
        f"- shadow_contains_orders_o_custkey_o_orderdate_rounds: {sum(1 for r in per_round.values() if _truthy(r.get('shadow_contains_orders_o_custkey_o_orderdate')))}",
    ]


def _summarize_overlay_metrics(rows: List[Dict[str, str]]) -> List[str]:
    opportunity = sum(_as_int(r.get("overlay_opportunity_rounds")) for r in rows)
    admitted = sum(_as_int(r.get("overlay_lane_admitted_rounds")) for r in rows)
    starvation = 0.0 if opportunity <= 0 else 1.0 - (float(admitted) / float(opportunity))
    block_reasons = Counter(
        str(r.get("replacement_overlay_block_reason", "")).strip()
        for r in rows
        if str(r.get("replacement_overlay_block_reason", "")).strip()
    )
    return [
        f"- replacement_overlay_applied_count_total: {_fmt(sum(_as_float(r.get('replacement_overlay_applied_count')) for r in rows))}",
        f"- replacement_overlay_block_reason_counts: {dict(block_reasons) if block_reasons else {}}",
        f"- overlay_opportunity_rounds: {opportunity}",
        f"- overlay_lane_admitted_rounds: {admitted}",
        f"- overlay_starvation_rate: {starvation:.4f}",
        f"- replacement_overlay_co_residency_count_total: {_fmt(sum(_as_float(r.get('replacement_overlay_co_residency_count')) for r in rows))}",
    ]


def _summarize_case(name: str, csv_path: Path, rows: List[Dict[str, str]], trace_rows: Optional[List[Dict[str, str]]] = None) -> List[str]:
    warnings: List[str] = []
    total_rounds = len(rows)
    timeout_count = sum(_as_int(r.get("timeout")) for r in rows)
    switched_count = sum(_as_int(r.get("switched")) for r in rows if "switched" in r)
    gen_counts = Counter(str(r.get("gen_mode", "")).strip() or "(blank)" for r in rows)
    new_values = [str(r.get("new", "")).strip() for r in rows if str(r.get("new", "")).strip()]
    unique_new = len(set(new_values))

    for r in rows:
        rid = _as_int(r.get("round"), -1)
        width2 = _as_float(r.get("width2_count"))
        eligible_seed = _as_float(r.get("eligible_seed_count"))
        mode = str(r.get("gen_mode", "")).strip()
        if rid in (0, 1) and width2 > 0:
            warnings.append(f"round {rid} has width2_count={_fmt(width2)}")
        if rid in (0, 1) and mode and mode != "probe":
            warnings.append(f"round {rid} has gen_mode={mode!r}, expected 'probe'")
        if width2 > 0 and eligible_seed <= 0:
            warnings.append(f"round {rid} has width2_count={_fmt(width2)} while eligible_seed_count=0")

    parse_fallback_rejects = sum(_as_float(r.get("rejected_growth_parse_fallback")) for r in rows)
    if parse_fallback_rejects > max(10.0, total_rounds * 0.5):
        warnings.append(f"rejected_growth_parse_fallback is high: {_fmt(parse_fallback_rejects)}")
    if timeout_count > 0:
        warnings.append(f"timeout count > 0: {timeout_count}")

    raw_max = max((_as_float(r.get("candidate_count_raw")) for r in rows), default=0.0)
    cand_max = max((_as_float(r.get("candidate_count")) for r in rows), default=0.0)
    if raw_max > 500:
        warnings.append(f"candidate_count_raw max looks large: {_fmt(raw_max)}")
    if cand_max > 200:
        warnings.append(f"candidate_count max looks large: {_fmt(cand_max)}")

    lines = [
        f"### {name}",
        "",
        f"- csv: `{csv_path}`",
        f"- total_rounds: {total_rounds}",
        f"- timeout_count: {timeout_count}",
        f"- total_what_if_calls: {_fmt(sum(_as_float(r.get('what_if_calls')) for r in rows))}",
        f"- gen_mode_counts: {dict(gen_counts)}",
        f"- unique_new_conf_count: {unique_new}",
        f"- new_conf_count: {len(new_values)}",
        f"- switched_count: {switched_count}",
    ]

    for col in NUMERIC_COLUMNS:
        vals = [_as_float(r.get(col)) for r in rows]
        lines.append(f"- {col}: mean={_fmt(_mean(vals))} max={_fmt(max(vals, default=0.0))}")
    for col in TOTAL_COLUMNS:
        vals = [_as_float(r.get(col)) for r in rows]
        lines.append(f"- {col}_total: {_fmt(sum(vals))}")
    lines.extend(_summarize_overlay_metrics(rows))
    lines.extend(_summarize_pair_supply_metrics(rows))
    lines.extend(_summarize_width2_trace(trace_rows))
    lines.extend(_summarize_shadow_trace(trace_rows))
    top_add = Counter(str(r.get("shadow_top_add_actions", "")).strip() for r in rows if str(r.get("shadow_top_add_actions", "")).strip())
    top_replace = Counter(str(r.get("shadow_top_replace_actions", "")).strip() for r in rows if str(r.get("shadow_top_replace_actions", "")).strip())
    if top_add:
        lines.append(f"- common_shadow_top_add_action_sets: {_top_counter(top_add, limit=3)}")
    if top_replace:
        lines.append(f"- common_shadow_top_replace_action_sets: {_top_counter(top_replace, limit=3)}")

    if warnings:
        lines.append("- warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")
    else:
        lines.append("- warnings: none")
    lines.append("")
    return lines


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 scripts/server/summarize_phase05.py <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(argv[1]).resolve()
    if not run_dir.is_dir():
        print(f"Run directory not found: {run_dir}", file=sys.stderr)
        return 1

    csv_paths = sorted(
        p for p in run_dir.rglob("*.csv")
        if not _under_preexisting_archive(p, run_dir) and _is_metrics_csv(p)
    )
    out = run_dir / "summary.md"
    lines = [
        "# Phase 0.5 First-Pass Summary",
        "",
        f"- run_dir: `{run_dir}`",
        f"- metrics_csv_files: {len(csv_paths)}",
        "",
    ]
    if not csv_paths:
        lines.append("No metrics CSV files found.")
    for path in csv_paths:
        rows = _read_rows(path)
        lines.extend(_summarize_case(_case_name(path, run_dir), path, rows, _trace_rows_for_case(path)))
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
