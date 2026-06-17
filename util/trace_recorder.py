# -*- coding: utf-8 -*-
"""TraceRecorder (Phase 0.3 / 0.4).

This module persists *per-round, per-index* trace rows so we can analyze
AdaSelect vs LiteSelect selection paths and oscillation causes.

Key properties
  - Default OFF (callers should only instantiate when enabled).
  - Default interest set = Old ∪ Appearing ∪ Evaluated ∪ Candidate ∪ Final.
  - Flush each row to survive kill/timeout.
"""

from __future__ import annotations

import ast
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set, Tuple


IndexKey = Tuple[str, Tuple[str, ...]]  # (table, (col1, col2, ...))


def _fmt_cols(cols: Tuple[str, ...]) -> str:
    return ",".join(cols)


def _sort_key(k: IndexKey) -> Tuple[str, int, Tuple[str, ...]]:
    return (k[0], len(k[1]), k[1])


def _fmt_index_key(k: IndexKey) -> str:
    return f"{k[0]}({_fmt_cols(k[1])})"


def _parse_index_key(value: Any) -> Optional[IndexKey]:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], tuple):
        return str(value[0]), tuple(str(c) for c in value[1])
    if not value:
        return None
    try:
        obj = ast.literal_eval(str(value))
    except Exception:
        return None
    if isinstance(obj, tuple) and len(obj) == 2:
        table = str(obj[0])
        cols = obj[1]
        if isinstance(cols, (tuple, list)):
            return table, tuple(str(c) for c in cols)
    return None


def covered_prefix_singles(pair: IndexKey, old_conf: Set[IndexKey], candidate_conf: Set[IndexKey]) -> Tuple[IndexKey, ...]:
    """Return same-table singles in old/candidate config that are components of a pair."""
    table, cols = pair
    if len(cols) < 2:
        return tuple()
    context = set(old_conf or set()) | set(candidate_conf or set())
    singles = []
    for col in cols:
        single = (table, (col,))
        if single in context:
            singles.append(single)
    return tuple(singles)


def _structural_pair_type(key: IndexKey, meta: Dict[str, Any], meta_map: Dict[IndexKey, Any]) -> str:
    if len(key[1]) != 2:
        return ""
    family = str(meta.get("family", "") or "")
    explicit_type = str(meta.get("structural_pair_type", "") or "")
    if explicit_type:
        return explicit_type
    seed_key = _parse_index_key(meta.get("seed_key", ""))
    seed_meta = meta_map.get(seed_key, {}) if seed_key is not None and isinstance(meta_map, dict) else {}
    seed_family = str(seed_meta.get("family", "") or "") if isinstance(seed_meta, dict) else ""
    if family == "EQ_RANGE" and seed_family == "JOIN_EQ1":
        return "JOIN_RANGE"
    if family == "EQ_EQ" and seed_family == "JOIN_EQ1":
        return "JOIN_EQ"
    return family


def _diagnostic_structural_pair_type(key: IndexKey, meta: Dict[str, Any], meta_map: Dict[IndexKey, Any]) -> str:
    if len(key[1]) != 2:
        return ""
    family = str(meta.get("family", "") or "")
    seed_family = str(meta.get("grow_seed_family", "") or "")
    if family == "EQ_RANGE" and seed_family == "JOIN_EQ1":
        return "JOIN_RANGE"
    if family == "EQ_EQ" and seed_family == "JOIN_EQ1":
        return "JOIN_EQ"
    return _structural_pair_type(key, meta, meta_map)


@dataclass
class TraceRecorder:
    """Append-only CSV trace recorder."""

    path: Path
    flush_each_row: bool = True

    _fh: Optional[Any] = None
    _writer: Optional[csv.DictWriter] = None

    # Base schema (stable across algorithms)
    FIELDS = [
        "round",
        "algo",
        "table",
        "cols",
        # per-round funnel stats (repeated on every row; easier to join/plot)
        "pruned_small_tables",
        "dml_tables_downweighted",
        "dml_weight_min",
        "dml_weight_max",
        # per-round stability stats (AdaSelect dead-zone support gate)
        "deadzone_old_support",
        "deadzone_blocked",
        "status",  # kept/added/dropped/rejected
        "in_old",
        "in_new",
        "in_eval",
        "in_appearing",
        "in_candidate",
        "rank",  # within eval-order if available
        "wdcg_score",
        "benefit",  # raw (algorithm-internal)
        "net_benefit",
        "obs_delta",
        "obs_src",
        "creation_cost",
        # G0-3 / Phase 0.5 meta
        "enum_mode",
        "family",
        "base_family",
        "merge_family",
        "merge_suffix_source",
        "compile_valid",
        "compile_pick_reason",
        "skip_reason",
        "table_row_count",
        "table_dml_ratio",
        "width_before_merge",
        "width_after_merge",
        "seed_key",
        "grow_seed_key",
        "grow_seed_family",
        "grow_seed_family_set",
        "seed_benefit",
        "seed_normalized_benefit",
        "seed_evaluated_count",
        "seed_positive_count",
        "seed_first_seen_round",
        "seed_last_seen_round",
        "seed_seen_rounds",
        "seed_last_obs_src",
        "seed_mature",
        "grow_reason",
        "rejected_growth_reason",
        "pair_family_vs_grow_reason_mismatch",
        "seed_family_missing",
        "join_seed_downgraded",
        "pair_fate",
        "covered_prefix_singles",
        "structural_pair_type",
        "diagnostic_structural_pair_type",
        "expected_structural_pair_type",
        "left_prefix_single",
        "component_singles",
        "left_prefix_in_old",
        "left_prefix_in_new",
        "left_prefix_in_candidate",
        "marginal_benefit",
        "replacement_benefit_raw",
        "replacement_benefit",
        "replacement_normalized_benefit",
        "replacement_creation_cost",
        "replacement_net_benefit",
        "replacement_probe_count",
        "replacement_what_if_calls",
        "replacement_hit_count",
        "replacement_ok_count",
        "replacement_fail_count",
        "replacement_diag_time",
        "structural_pair_quota",
        "structural_pair_eval_count",
        "structural_pair_eval_selected_keys",
        "structural_pair_eval_budgeted_out_count",
        "structural_pair_eval_lane_enabled",
        "shadow_action_key",
        "shadow_action_type",
        "shadow_action_normalized_benefit",
        "shadow_action_transition_cost",
        "shadow_action_utility",
        "shadow_action_utility_source",
        "shadow_action_utility_scale_basis",
        "shadow_action_in_naive",
        "shadow_action_in_conflict_aware",
        "shadow_action_count",
        "shadow_add_action_count",
        "shadow_replace_action_count",
        "shadow_greedy_action_count_after_dedup",
        "shadow_duplicate_target_action_count",
        "shadow_top_add_actions",
        "shadow_top_replace_actions",
        "shadow_greedy_config_naive",
        "shadow_greedy_actions_naive",
        "naive_replacement_count",
        "naive_add_count",
        "naive_prefix_missing_add_count",
        "naive_pair_count",
        "shadow_greedy_config_conflict_aware",
        "shadow_greedy_actions_conflict_aware",
        "shadow_greedy_config_stale",
        "shadow_greedy_actions_stale",
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
        "shadow_naive_only_actions",
        "shadow_conflict_aware_only_actions",
        "replacement_overlay_enabled",
        "replacement_overlay_applied_count",
        "replacement_overlay_selected_action",
        "replacement_overlay_pair",
        "replacement_overlay_prefix",
        "replacement_overlay_utility",
        "replacement_overlay_before_conf",
        "replacement_overlay_after_conf",
        "replacement_overlay_blocked_count",
        "replacement_overlay_block_reason",
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
        "candidate_generation_mode",
        "cg_width2_pre_cap_count",
        "cg_width2_post_cap_count",
        "cg_width2_dropped_round_count",
        "cg_width2_fairness_added_count",
        "cg_width2_fairness_added_pairs",
        "cg_target_pair_postround_coverage_count",
        "cg_candidate_budget_delta",
        "pair_supply_ceiling_enabled",
        "pair_supply_ceiling_width2_added_perquery",
        "pair_supply_ceiling_width2_added_round",
        "pair_supply_ceiling_width2_survived_count",
        "pair_supply_ceiling_target_pairs_recovered",
        "pair_supply_ceiling_candidate_count_delta",
        "pair_supply_ceiling_examples",
        "pair_supply_fairness_enabled",
        "pair_supply_per_table_width2_reserve",
        "pair_supply_round_width2_reserve",
        "pair_supply_fairness_applied_count",
        "pair_supply_fairness_rescued_width2_count",
        "pair_supply_fairness_rescued_pairs",
        "pair_supply_fairness_rescued_by_table",
        "pair_supply_fairness_displaced_width1_count",
        "pair_supply_fairness_displaced_width1_keys",
        "pair_supply_fairness_columnset_dedup_count",
        "pair_supply_fairness_block_reason",
        "pair_supply_fairness_candidate_count_delta",
        "pair_supply_fairness_target_pairs_recovered",
        "pair_supply_fairness_target_pairs_recovered_examples",
        "fairness_eval_lane_enabled",
        "fairness_eval_lane_quota",
        "fairness_eval_lane_candidate_count",
        "fairness_eval_lane_evaluated_count",
        "fairness_eval_lane_evaluated_pairs",
        "fairness_eval_lane_replacement_diag_count",
        "fairness_eval_lane_skipped_already_evaluated_count",
        "fairness_eval_lane_budgeted_out_count",
        "fairness_eval_lane_what_if_calls",
        "fairness_eval_lane_replacement_what_if_calls",
        "fairness_eval_lane_shadowing_revealed_count",
        "fairness_eval_lane_nonbeneficial_count",
        "target_pair_count",
        "target_pair_prequery_coverage_count",
        "target_pair_postquery_coverage_count",
        "target_pair_preround_coverage_count",
        "target_pair_postround_coverage_count",
        "target_pair_lane_admitted_count",
        "target_pair_selected_count",
        "target_pair_final_count",
        "target_pair_missing_examples",
        "target_pair_dropped_perquery_examples",
        "target_pair_dropped_round_examples",
        "target_pair_fate_summary",
        "mat_pair_key",
        "mat_pair_in_postround",
        "mat_pair_in_candidate_conf",
        "mat_pair_in_final_conf",
        "mat_pair_evaluated",
        "mat_pair_main_raw_benefit",
        "mat_pair_main_normalized_benefit",
        "mat_pair_main_net_utility",
        "mat_pair_creation_cost",
        "mat_replacement_diag_available",
        "mat_replacement_net_benefit",
        "mat_replacement_utility",
        "mat_left_prefix",
        "mat_left_prefix_in_old_conf",
        "mat_left_prefix_in_candidate_conf",
        "mat_left_prefix_in_final_conf",
        "mat_left_prefix_net_utility",
        "mat_gap_reason",
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
        "materialization_gap_not_postround_examples",
        "materialization_gap_eval_gap_examples",
        "materialization_gap_prefix_shadowing_examples",
        "materialization_gap_replacement_positive_main_nonpositive_examples",
        "materialization_gap_eval_confirmed_nonbeneficial_examples",
        "materialization_gap_main_positive_not_selected_examples",
        "replacement_overlay_co_residency_count",
        # AdaSelect-only (best effort; blank for LiteSelect)
        "lambda",
        "lambda_shadow",
        "rsfe",
        "mad",
        "ts",
        "decision_ratio",
        "decision_old_benefit",
        "decision_new_benefit",
    ]

    def __enter__(self) -> "TraceRecorder":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.FIELDS)
        # Write header only if file is new/empty
        if self._fh.tell() == 0:
            self._writer.writeheader()
            if self.flush_each_row:
                self._fh.flush()
        return self

    def close(self) -> None:
        try:
            if self._fh:
                self._fh.flush()
        finally:
            try:
                if self._fh:
                    self._fh.close()
            finally:
                self._fh = None
                self._writer = None

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def record_round(
        self,
        round_id: int,
        old_conf: Set[IndexKey],
        new_conf: Set[IndexKey],
        evaluated_set: Optional[Set[IndexKey]] = None,
        tuner: Any = None,
        algo_name: str = "",
        interest_set: Optional[Set[IndexKey]] = None,
        **_ignored: Any,
    ) -> None:
        """Write trace rows for one round.

        Parameters
        ----------
        evaluated_set:
          Indexes that actually entered the what-if evaluation this round.
        tuner:
          Optional, used to fetch per-index signals (benefit, lambda/ts...).
        interest_set:
          If None, defaults to Old ∪ Appearing ∪ Evaluated ∪ Candidate ∪ Final.
        """
        if self._writer is None:
            raise RuntimeError("TraceRecorder is not opened. Use 'with TraceRecorder(...) as tr:'")

        ev: Set[IndexKey] = set(evaluated_set or set())
        appearing: Set[IndexKey] = set()
        candidate: Set[IndexKey] = set()
        compile_rejected: Set[IndexKey] = set()
        meta_map: Dict[IndexKey, Any] = {}
        pair_fate_map: Dict[IndexKey, str] = {}
        materialization_gap_map: Dict[IndexKey, Dict[str, Any]] = {}
        final_conf_logged: Set[IndexKey] = set(new_conf or set())
        if tuner is not None:
            try:
                appearing = set(getattr(tuner, "_last_appearing_set", set()) or set())
            except Exception:
                appearing = set()
            try:
                candidate = set(getattr(tuner, "_last_candidate_conf", set()) or set())
            except Exception:
                candidate = set()
            try:
                final_conf_logged = set(getattr(tuner, "_last_final_conf", final_conf_logged) or final_conf_logged)
            except Exception:
                final_conf_logged = set(new_conf or set())
            try:
                _gen = getattr(tuner, "_wdcg_gen", None)
                if _gen is not None and isinstance(getattr(getattr(_gen, "enum", None), "last_meta", None), dict):
                    meta_map = getattr(getattr(_gen, "enum", None), "last_meta", {}) or {}
                    compile_rejected = {k for k, m in meta_map.items() if isinstance(m, dict) and m.get("compile_valid", None) is False}
            except Exception:
                meta_map = {}
                compile_rejected = set()
            try:
                pair_fate_map = getattr(tuner, "_last_pair_fate_map", {}) or {}
            except Exception:
                pair_fate_map = {}
            try:
                materialization_gap_map = getattr(tuner, "_last_materialization_gap_map", {}) or {}
            except Exception:
                materialization_gap_map = {}
        width2_meta = {
            k for k in meta_map
            if isinstance(k, tuple) and len(k) == 2 and isinstance(k[1], tuple) and len(k[1]) == 2
        } if isinstance(meta_map, dict) else set()
        pair_fate_keys = set(pair_fate_map) if isinstance(pair_fate_map, dict) else set()
        materialization_gap_keys = set(materialization_gap_map) if isinstance(materialization_gap_map, dict) else set()
        interest: Set[IndexKey] = set(interest_set) if interest_set is not None else (set(old_conf) | final_conf_logged | ev | appearing | candidate | compile_rejected | width2_meta | pair_fate_keys | materialization_gap_keys)

        # Per-round WDCG funnel stats (optional; repeat on each row)
        wdcg_stats: Dict[str, Any] = {}
        if tuner is not None:
            try:
                wdcg_stats = getattr(tuner, "_last_wdcg_stats", {}) or {}
            except Exception:
                wdcg_stats = {}
        pruned_small_tables = wdcg_stats.get("pruned_small_tables", "")
        dml_tables_downweighted = wdcg_stats.get("dml_tables_downweighted", "")
        dml_weight_min = wdcg_stats.get("dml_weight_min", "")
        dml_weight_max = wdcg_stats.get("dml_weight_max", "")

        # Dead-zone support-gate stats (AdaSelect). Repeated per row.
        deadzone_old_support = ""
        deadzone_blocked = ""
        if tuner is not None:
            try:
                dz = getattr(tuner, "_last_deadzone_stats", {}) or {}
                deadzone_old_support = dz.get("deadzone_old_support", "")
                deadzone_blocked = dz.get("deadzone_blocked", "")
            except Exception:
                deadzone_old_support = ""
                deadzone_blocked = ""

        if not algo_name and tuner is not None:
            try:
                algo_name = tuner.__class__.__name__
            except Exception:
                algo_name = ""

        # Optional ranking within evaluation order (if algorithm exposes it)
        rank_map: Dict[IndexKey, int] = {}
        if tuner is not None:
            order = getattr(tuner, "_last_eval_order", None)
            if isinstance(order, list):
                for i, k in enumerate(order, start=1):
                    if k not in rank_map:
                        rank_map[k] = i

        wdcg_score_map: Dict[IndexKey, Any] = {}
        net_benefit_map: Dict[IndexKey, Any] = {}
        obs_delta_map: Dict[IndexKey, Any] = {}
        obs_src_map: Dict[IndexKey, Any] = {}
        replacement_map: Dict[IndexKey, Dict[str, Any]] = {}
        shadow_action_map: Dict[IndexKey, Dict[str, Any]] = {}
        shadow_naive_actions: Set[str] = set()
        shadow_conflict_actions: Set[str] = set()
        decision_stats: Dict[str, Any] = {}
        if tuner is not None:
            try:
                wdcg_score_map = getattr(tuner, "_last_wdcg_score_map", {}) or {}
            except Exception:
                wdcg_score_map = {}
            try:
                net_benefit_map = getattr(tuner, "_last_net_benefit_map", {}) or {}
            except Exception:
                net_benefit_map = {}
            try:
                obs_delta_map = getattr(tuner, "_last_obs_delta_map", {}) or {}
            except Exception:
                obs_delta_map = {}
            try:
                obs_src_map = getattr(tuner, "_last_obs_src_map", {}) or {}
            except Exception:
                obs_src_map = {}
            try:
                replacement_map = getattr(tuner, "_last_structural_pair_replacement_map", {}) or {}
            except Exception:
                replacement_map = {}
            try:
                shadow_rows = getattr(tuner, "_last_shadow_action_rows", []) or []
                for action in shadow_rows:
                    if not isinstance(action, dict):
                        continue
                    key = action.get("index_key", None)
                    if isinstance(key, tuple) and key not in shadow_action_map:
                        shadow_action_map[key] = action
                shadow_naive_actions = {
                    a.strip()
                    for a in str(wdcg_stats.get("shadow_greedy_actions_naive", "") or "").split("|")
                    if a.strip()
                }
                shadow_conflict_actions = {
                    a.strip()
                    for a in str(wdcg_stats.get("shadow_greedy_actions_conflict_aware", "") or "").split("|")
                    if a.strip()
                }
            except Exception:
                shadow_action_map = {}
                shadow_naive_actions = set()
                shadow_conflict_actions = set()
            try:
                decision_stats = getattr(tuner, "_last_decision_stats", {}) or {}
            except Exception:
                decision_stats = {}

        tbl_rows: Dict[str, Any] = {}
        tbl_dml: Dict[str, Any] = {}
        if tuner is not None:
            try:
                _gen = getattr(tuner, "_wdcg_gen", None)
                if _gen is not None:
                    tbl_rows = getattr(_gen, "_tbl_rows", {}) or {}
                    tbl_dml = getattr(_gen, "_tbl_dml_ema", getattr(_gen, "_tbl_dml_ratio", {})) or {}
            except Exception:
                tbl_rows = {}
                tbl_dml = {}

        for k in sorted(interest, key=_sort_key):
            in_old = k in old_conf
            in_new = k in final_conf_logged
            in_eval = k in ev
            in_appearing = k in appearing
            in_candidate = k in candidate
            meta = meta_map.get(k, {}) if isinstance(meta_map, dict) else {}
            compile_valid = meta.get("compile_valid", "") if isinstance(meta, dict) else ""
            compile_pick_reason = meta.get("compile_pick_reason", "") if isinstance(meta, dict) else ""
            skip_reason = meta.get("skip_reason", "") if isinstance(meta, dict) else ""

            if compile_valid is False and not in_new:
                status = "compile_rejected"
            elif in_old and in_new:
                status = "kept"
            elif (not in_old) and in_new:
                status = "added"
            elif in_old and (not in_new):
                status = "dropped"
            else:
                # Not chosen; if it was evaluated, it's informative for oscillation.
                status = "rejected" if in_eval else "other"

            # Best-effort per-index signals
            benefit = ""
            if tuner is not None and hasattr(tuner, "columns_benefit"):
                try:
                    benefit = float(getattr(tuner, "columns_benefit").get(k, ""))
                except Exception:
                    benefit = ""

            wdcg_score = ""
            try:
                if k in wdcg_score_map:
                    wdcg_score = float(wdcg_score_map.get(k, ""))
                elif isinstance(meta, dict) and meta.get("score", "") != "":
                    wdcg_score = float(meta.get("score", ""))
            except Exception:
                wdcg_score = ""

            covered = ""
            structural_pair_type = ""
            diagnostic_structural_pair_type = ""
            left_prefix_single = ""
            component_singles = ""
            left_prefix_in_old = ""
            left_prefix_in_new = ""
            left_prefix_in_candidate = ""
            marginal_benefit = ""
            replacement_benefit_raw = ""
            replacement_benefit = ""
            replacement_normalized_benefit = ""
            replacement_creation_cost = ""
            replacement_net_benefit = ""
            replacement_hit_count = ""
            replacement_ok_count = ""
            replacement_fail_count = ""
            replacement_diag_time = ""
            mat = materialization_gap_map.get(k, {}) if isinstance(materialization_gap_map, dict) else {}
            if not isinstance(mat, dict):
                mat = {}
            shadow_action = shadow_action_map.get(k, {}) if isinstance(shadow_action_map, dict) else {}
            shadow_action_key = ""
            shadow_action_type = ""
            shadow_action_normalized_benefit = ""
            shadow_action_transition_cost = ""
            shadow_action_utility = ""
            shadow_action_utility_source = ""
            shadow_action_utility_scale_basis = ""
            shadow_action_in_naive = ""
            shadow_action_in_conflict_aware = ""
            if isinstance(shadow_action, dict) and shadow_action:
                shadow_action_key = shadow_action.get("action_key", "")
                shadow_action_type = shadow_action.get("action_type", "")
                shadow_action_normalized_benefit = shadow_action.get("action_normalized_benefit", "")
                shadow_action_transition_cost = shadow_action.get("action_normalized_transition_cost", shadow_action.get("action_transition_cost", ""))
                shadow_action_utility = shadow_action.get("action_utility", "")
                shadow_action_utility_source = shadow_action.get("utility_source", "")
                shadow_action_utility_scale_basis = shadow_action.get("utility_scale_basis", "")
                shadow_action_in_naive = 1 if shadow_action_key in shadow_naive_actions else 0
                shadow_action_in_conflict_aware = 1 if shadow_action_key in shadow_conflict_actions else 0
            if len(k[1]) == 2:
                try:
                    covered = ";".join(_fmt_index_key(x) for x in covered_prefix_singles(k, set(old_conf), candidate))
                except Exception:
                    covered = ""
                try:
                    structural_pair_type = _structural_pair_type(k, meta if isinstance(meta, dict) else {}, meta_map)
                    diagnostic_structural_pair_type = _diagnostic_structural_pair_type(k, meta if isinstance(meta, dict) else {}, meta_map)
                except Exception:
                    structural_pair_type = ""
                    diagnostic_structural_pair_type = ""
                repl = replacement_map.get(k, {}) if isinstance(replacement_map, dict) else {}
                if isinstance(repl, dict):
                    lp = repl.get("left_prefix_single", None)
                    comps = tuple(repl.get("component_singles", tuple()) or tuple())
                    if lp:
                        try:
                            left_prefix_single = _fmt_index_key(lp)
                            left_prefix_in_old = 1 if lp in old_conf else 0
                            left_prefix_in_new = 1 if lp in final_conf_logged else 0
                            left_prefix_in_candidate = 1 if lp in candidate else 0
                        except Exception:
                            left_prefix_single = ""
                    if comps:
                        try:
                            component_singles = ";".join(_fmt_index_key(x) for x in comps)
                        except Exception:
                            component_singles = ""
                    marginal_benefit = repl.get("marginal_benefit", "")
                    replacement_benefit_raw = repl.get("replacement_benefit_raw", "")
                    replacement_benefit = repl.get("replacement_benefit", "")
                    replacement_normalized_benefit = repl.get("replacement_normalized_benefit", "")
                    replacement_creation_cost = repl.get("replacement_creation_cost", "")
                    replacement_net_benefit = repl.get("replacement_net_benefit", "")
                    replacement_hit_count = repl.get("replacement_hit_count", "")
                    replacement_ok_count = repl.get("replacement_ok_count", "")
                    replacement_fail_count = repl.get("replacement_fail_count", "")
                    replacement_diag_time = repl.get("replacement_diag_time", "")

            net_benefit = ""
            try:
                if k in net_benefit_map:
                    net_benefit = float(net_benefit_map.get(k, ""))
            except Exception:
                net_benefit = ""

            obs_delta = ""
            try:
                if k in obs_delta_map:
                    obs_delta = float(obs_delta_map.get(k, ""))
            except Exception:
                obs_delta = ""

            obs_src = obs_src_map.get(k, "") if isinstance(obs_src_map, dict) else ""

            creation_cost = ""
            if tuner is not None and hasattr(tuner, "_creation_cost"):
                try:
                    creation_cost = float(tuner._creation_cost(k))
                except Exception:
                    creation_cost = ""

            # AdaSelect-only (safe to leave blank)
            lam = lam_shadow = rsfe = mad = ts = ""
            if tuner is not None:
                try:
                    if hasattr(tuner, "idx_alphas"):
                        lam = tuner.idx_alphas.get(k, "")
                    if hasattr(tuner, "idx_alphas_shadow"):
                        lam_shadow = tuner.idx_alphas_shadow.get(k, "")
                    if hasattr(tuner, "idx_error_smooth"):
                        rsfe = tuner.idx_error_smooth.get(k, "")
                    if hasattr(tuner, "idx_abs_error_smooth"):
                        mad = tuner.idx_abs_error_smooth.get(k, "")
                    if mad not in (None, ""):
                        m = float(mad)
                        if m > 1e-9 and rsfe not in (None, ""):
                            ts = abs(float(rsfe)) / (m + 1e-9)
                except Exception:
                    # keep blanks
                    pass

            row = {
                "round": int(round_id),
                "algo": str(algo_name),
                "table": k[0],
                "cols": _fmt_cols(k[1]),
                "pruned_small_tables": pruned_small_tables,
                "dml_tables_downweighted": dml_tables_downweighted,
                "dml_weight_min": dml_weight_min,
                "dml_weight_max": dml_weight_max,
                "deadzone_old_support": deadzone_old_support,
                "deadzone_blocked": deadzone_blocked,
                "status": status,
                "in_old": 1 if in_old else 0,
                "in_new": 1 if in_new else 0,
                "in_eval": 1 if in_eval else 0,
                "in_appearing": 1 if in_appearing else 0,
                "in_candidate": 1 if in_candidate else 0,
                "rank": rank_map.get(k, ""),
                "wdcg_score": wdcg_score,
                "benefit": benefit,
                "net_benefit": net_benefit,
                "obs_delta": obs_delta,
                "obs_src": obs_src,
                "creation_cost": creation_cost,
                "enum_mode": wdcg_stats.get("wdcg_enum_mode", getattr(tuner, "wdcg_enum_mode", "") if tuner is not None else ""),
                "family": meta.get("family", "") if isinstance(meta, dict) else "",
                "base_family": meta.get("base_family", meta.get("family", "")) if isinstance(meta, dict) else "",
                "merge_family": meta.get("merge_family", "") if isinstance(meta, dict) else "",
                "merge_suffix_source": meta.get("merge_suffix_source", "") if isinstance(meta, dict) else "",
                "compile_valid": compile_valid,
                "compile_pick_reason": compile_pick_reason,
                "skip_reason": skip_reason,
                "table_row_count": tbl_rows.get(k[0], "") if isinstance(tbl_rows, dict) else "",
                "table_dml_ratio": tbl_dml.get(k[0], "") if isinstance(tbl_dml, dict) else "",
                "width_before_merge": meta.get("width_before_merge", len(k[1])) if isinstance(meta, dict) else len(k[1]),
                "width_after_merge": meta.get("width_after_merge", len(k[1])) if isinstance(meta, dict) else len(k[1]),
                "seed_key": repr(meta.get("seed_key", "")) if isinstance(meta, dict) and meta.get("seed_key", "") else "",
                "grow_seed_key": repr(meta.get("grow_seed_key", "")) if isinstance(meta, dict) and meta.get("grow_seed_key", "") else "",
                "grow_seed_family": meta.get("grow_seed_family", "") if isinstance(meta, dict) else "",
                "grow_seed_family_set": "|".join(str(x) for x in (meta.get("grow_seed_family_set", []) if isinstance(meta, dict) else []) if str(x)),
                "seed_benefit": meta.get("seed_benefit", "") if isinstance(meta, dict) else "",
                "seed_normalized_benefit": meta.get("seed_normalized_benefit", "") if isinstance(meta, dict) else "",
                "seed_evaluated_count": meta.get("seed_evaluated_count", "") if isinstance(meta, dict) else "",
                "seed_positive_count": meta.get("seed_positive_count", "") if isinstance(meta, dict) else "",
                "seed_first_seen_round": meta.get("seed_first_seen_round", "") if isinstance(meta, dict) else "",
                "seed_last_seen_round": meta.get("seed_last_seen_round", "") if isinstance(meta, dict) else "",
                "seed_seen_rounds": repr(meta.get("seed_seen_rounds", "")) if isinstance(meta, dict) and meta.get("seed_seen_rounds", "") != "" else "",
                "seed_last_obs_src": meta.get("seed_last_obs_src", "") if isinstance(meta, dict) else "",
                "seed_mature": meta.get("seed_mature", "") if isinstance(meta, dict) else "",
                "grow_reason": meta.get("grow_reason", "") if isinstance(meta, dict) else "",
                "rejected_growth_reason": meta.get("rejected_growth_reason", "") if isinstance(meta, dict) else "",
                "pair_family_vs_grow_reason_mismatch": meta.get("pair_family_vs_grow_reason_mismatch", "") if isinstance(meta, dict) else "",
                "seed_family_missing": meta.get("seed_family_missing", "") if isinstance(meta, dict) else "",
                "join_seed_downgraded": meta.get("join_seed_downgraded", "") if isinstance(meta, dict) else "",
                "pair_fate": pair_fate_map.get(k, "") if isinstance(pair_fate_map, dict) else "",
                "covered_prefix_singles": covered,
                "structural_pair_type": structural_pair_type,
                "diagnostic_structural_pair_type": diagnostic_structural_pair_type,
                "expected_structural_pair_type": meta.get("expected_structural_pair_type", "") if isinstance(meta, dict) else "",
                "left_prefix_single": left_prefix_single,
                "component_singles": component_singles,
                "left_prefix_in_old": left_prefix_in_old,
                "left_prefix_in_new": left_prefix_in_new,
                "left_prefix_in_candidate": left_prefix_in_candidate,
                "marginal_benefit": marginal_benefit,
                "replacement_benefit_raw": replacement_benefit_raw,
                "replacement_benefit": replacement_benefit,
                "replacement_normalized_benefit": replacement_normalized_benefit,
                "replacement_creation_cost": replacement_creation_cost,
                "replacement_net_benefit": replacement_net_benefit,
                "replacement_probe_count": wdcg_stats.get("replacement_probe_count", ""),
                "replacement_what_if_calls": wdcg_stats.get("replacement_what_if_calls", ""),
                "replacement_hit_count": replacement_hit_count,
                "replacement_ok_count": replacement_ok_count,
                "replacement_fail_count": replacement_fail_count,
                "replacement_diag_time": replacement_diag_time,
                "structural_pair_quota": wdcg_stats.get("structural_pair_quota", ""),
                "structural_pair_eval_count": wdcg_stats.get("structural_pair_eval_count", ""),
                "structural_pair_eval_selected_keys": wdcg_stats.get("structural_pair_eval_selected_keys", ""),
                "structural_pair_eval_budgeted_out_count": wdcg_stats.get("structural_pair_eval_budgeted_out_count", ""),
                "structural_pair_eval_lane_enabled": wdcg_stats.get("structural_pair_eval_lane_enabled", ""),
                "shadow_action_key": shadow_action_key,
                "shadow_action_type": shadow_action_type,
                "shadow_action_normalized_benefit": shadow_action_normalized_benefit,
                "shadow_action_transition_cost": shadow_action_transition_cost,
                "shadow_action_utility": shadow_action_utility,
                "shadow_action_utility_source": shadow_action_utility_source,
                "shadow_action_utility_scale_basis": shadow_action_utility_scale_basis,
                "shadow_action_in_naive": shadow_action_in_naive,
                "shadow_action_in_conflict_aware": shadow_action_in_conflict_aware,
                "shadow_action_count": wdcg_stats.get("shadow_action_count", ""),
                "shadow_add_action_count": wdcg_stats.get("shadow_add_action_count", ""),
                "shadow_replace_action_count": wdcg_stats.get("shadow_replace_action_count", ""),
                "shadow_greedy_action_count_after_dedup": wdcg_stats.get("shadow_greedy_action_count_after_dedup", ""),
                "shadow_duplicate_target_action_count": wdcg_stats.get("shadow_duplicate_target_action_count", ""),
                "shadow_top_add_actions": wdcg_stats.get("shadow_top_add_actions", ""),
                "shadow_top_replace_actions": wdcg_stats.get("shadow_top_replace_actions", ""),
                "shadow_greedy_config_naive": wdcg_stats.get("shadow_greedy_config_naive", ""),
                "shadow_greedy_actions_naive": wdcg_stats.get("shadow_greedy_actions_naive", ""),
                "naive_replacement_count": wdcg_stats.get("naive_replacement_count", ""),
                "naive_add_count": wdcg_stats.get("naive_add_count", ""),
                "naive_prefix_missing_add_count": wdcg_stats.get("naive_prefix_missing_add_count", ""),
                "naive_pair_count": wdcg_stats.get("naive_pair_count", ""),
                "shadow_greedy_config_conflict_aware": wdcg_stats.get("shadow_greedy_config_conflict_aware", ""),
                "shadow_greedy_actions_conflict_aware": wdcg_stats.get("shadow_greedy_actions_conflict_aware", ""),
                "shadow_greedy_config_stale": wdcg_stats.get("shadow_greedy_config_stale", ""),
                "shadow_greedy_actions_stale": wdcg_stats.get("shadow_greedy_actions_stale", ""),
                "stale_prefix_missing_count": wdcg_stats.get("stale_prefix_missing_count", ""),
                "shadow_transition_add_count": wdcg_stats.get("shadow_transition_add_count", ""),
                "shadow_transition_drop_count": wdcg_stats.get("shadow_transition_drop_count", ""),
                "shadow_transition_action_count": wdcg_stats.get("shadow_transition_action_count", ""),
                "shadow_pair_count": wdcg_stats.get("shadow_pair_count", ""),
                "shadow_replacement_count": wdcg_stats.get("shadow_replacement_count", ""),
                "shadow_diff_from_active_count": wdcg_stats.get("shadow_diff_from_active_count", ""),
                "shadow_diff_from_candidate_count": wdcg_stats.get("shadow_diff_from_candidate_count", ""),
                "shadow_contains_lineitem_l_partkey_l_shipdate": wdcg_stats.get("shadow_contains_lineitem_l_partkey_l_shipdate", ""),
                "shadow_contains_orders_o_custkey_o_orderdate": wdcg_stats.get("shadow_contains_orders_o_custkey_o_orderdate", ""),
                "shadow_naive_vs_conflict_action_diff_count": wdcg_stats.get("shadow_naive_vs_conflict_action_diff_count", ""),
                "shadow_naive_vs_conflict_config_diff_count": wdcg_stats.get("shadow_naive_vs_conflict_config_diff_count", ""),
                "shadow_naive_only_actions": wdcg_stats.get("shadow_naive_only_actions", ""),
                "shadow_conflict_aware_only_actions": wdcg_stats.get("shadow_conflict_aware_only_actions", ""),
                "replacement_overlay_enabled": wdcg_stats.get("replacement_overlay_enabled", ""),
                "replacement_overlay_applied_count": wdcg_stats.get("replacement_overlay_applied_count", ""),
                "replacement_overlay_selected_action": wdcg_stats.get("replacement_overlay_selected_action", ""),
                "replacement_overlay_pair": wdcg_stats.get("replacement_overlay_pair", ""),
                "replacement_overlay_prefix": wdcg_stats.get("replacement_overlay_prefix", ""),
                "replacement_overlay_utility": wdcg_stats.get("replacement_overlay_utility", ""),
                "replacement_overlay_before_conf": wdcg_stats.get("replacement_overlay_before_conf", ""),
                "replacement_overlay_after_conf": wdcg_stats.get("replacement_overlay_after_conf", ""),
                "replacement_overlay_blocked_count": wdcg_stats.get("replacement_overlay_blocked_count", ""),
                "replacement_overlay_block_reason": wdcg_stats.get("replacement_overlay_block_reason", ""),
                "replacement_overlay_diff_from_topk_count": wdcg_stats.get("replacement_overlay_diff_from_topk_count", ""),
                "overlay_opportunity_rounds": wdcg_stats.get("overlay_opportunity_rounds", ""),
                "overlay_lane_admitted_rounds": wdcg_stats.get("overlay_lane_admitted_rounds", ""),
                "overlay_opportunity_pair_count": wdcg_stats.get("overlay_opportunity_pair_count", ""),
                "overlay_lane_admitted_pair_count": wdcg_stats.get("overlay_lane_admitted_pair_count", ""),
                "overlay_blocked_by_lane_count": wdcg_stats.get("overlay_blocked_by_lane_count", ""),
                "overlay_blocked_by_eligibility_count": wdcg_stats.get("overlay_blocked_by_eligibility_count", ""),
                "overlay_fired_pair_count": wdcg_stats.get("overlay_fired_pair_count", ""),
                "pair_fate_universe_count": wdcg_stats.get("pair_fate_universe_count", ""),
                "pair_fate_dropped_perquery_cap_count": wdcg_stats.get("pair_fate_dropped_perquery_cap_count", ""),
                "pair_fate_dropped_round_cap_count": wdcg_stats.get("pair_fate_dropped_round_cap_count", ""),
                "pair_fate_generated_not_in_overlay_opportunity_count": wdcg_stats.get("pair_fate_generated_not_in_overlay_opportunity_count", ""),
                "pair_fate_in_opportunity_blocked_by_lane_count": wdcg_stats.get("pair_fate_in_opportunity_blocked_by_lane_count", ""),
                "pair_fate_lane_admitted_blocked_by_eligibility_count": wdcg_stats.get("pair_fate_lane_admitted_blocked_by_eligibility_count", ""),
                "pair_fate_lane_admitted_overlay_disabled_count": wdcg_stats.get("pair_fate_lane_admitted_overlay_disabled_count", ""),
                "pair_fate_lane_admitted_fired_count": wdcg_stats.get("pair_fate_lane_admitted_fired_count", ""),
                "pair_fate_not_generated_other_count": wdcg_stats.get("pair_fate_not_generated_other_count", ""),
                "candidate_generation_mode": wdcg_stats.get("candidate_generation_mode", ""),
                "cg_width2_pre_cap_count": wdcg_stats.get("cg_width2_pre_cap_count", ""),
                "cg_width2_post_cap_count": wdcg_stats.get("cg_width2_post_cap_count", ""),
                "cg_width2_dropped_round_count": wdcg_stats.get("cg_width2_dropped_round_count", ""),
                "cg_width2_fairness_added_count": wdcg_stats.get("cg_width2_fairness_added_count", ""),
                "cg_width2_fairness_added_pairs": wdcg_stats.get("cg_width2_fairness_added_pairs", ""),
                "cg_target_pair_postround_coverage_count": wdcg_stats.get("cg_target_pair_postround_coverage_count", ""),
                "cg_candidate_budget_delta": wdcg_stats.get("cg_candidate_budget_delta", ""),
                "pair_supply_ceiling_enabled": wdcg_stats.get("pair_supply_ceiling_enabled", ""),
                "pair_supply_ceiling_width2_added_perquery": wdcg_stats.get("pair_supply_ceiling_width2_added_perquery", ""),
                "pair_supply_ceiling_width2_added_round": wdcg_stats.get("pair_supply_ceiling_width2_added_round", ""),
                "pair_supply_ceiling_width2_survived_count": wdcg_stats.get("pair_supply_ceiling_width2_survived_count", ""),
                "pair_supply_ceiling_target_pairs_recovered": wdcg_stats.get("pair_supply_ceiling_target_pairs_recovered", ""),
                "pair_supply_ceiling_candidate_count_delta": wdcg_stats.get("pair_supply_ceiling_candidate_count_delta", ""),
                "pair_supply_ceiling_examples": wdcg_stats.get("pair_supply_ceiling_examples", ""),
                "pair_supply_fairness_enabled": wdcg_stats.get("pair_supply_fairness_enabled", ""),
                "pair_supply_per_table_width2_reserve": wdcg_stats.get("pair_supply_per_table_width2_reserve", ""),
                "pair_supply_round_width2_reserve": wdcg_stats.get("pair_supply_round_width2_reserve", ""),
                "pair_supply_fairness_applied_count": wdcg_stats.get("pair_supply_fairness_applied_count", ""),
                "pair_supply_fairness_rescued_width2_count": wdcg_stats.get("pair_supply_fairness_rescued_width2_count", ""),
                "pair_supply_fairness_rescued_pairs": wdcg_stats.get("pair_supply_fairness_rescued_pairs", ""),
                "pair_supply_fairness_rescued_by_table": wdcg_stats.get("pair_supply_fairness_rescued_by_table", ""),
                "pair_supply_fairness_displaced_width1_count": wdcg_stats.get("pair_supply_fairness_displaced_width1_count", ""),
                "pair_supply_fairness_displaced_width1_keys": wdcg_stats.get("pair_supply_fairness_displaced_width1_keys", ""),
                "pair_supply_fairness_columnset_dedup_count": wdcg_stats.get("pair_supply_fairness_columnset_dedup_count", ""),
                "pair_supply_fairness_block_reason": wdcg_stats.get("pair_supply_fairness_block_reason", ""),
                "pair_supply_fairness_candidate_count_delta": wdcg_stats.get("pair_supply_fairness_candidate_count_delta", ""),
                "pair_supply_fairness_target_pairs_recovered": wdcg_stats.get("pair_supply_fairness_target_pairs_recovered", ""),
                "pair_supply_fairness_target_pairs_recovered_examples": wdcg_stats.get("pair_supply_fairness_target_pairs_recovered_examples", ""),
                "fairness_eval_lane_enabled": wdcg_stats.get("fairness_eval_lane_enabled", ""),
                "fairness_eval_lane_quota": wdcg_stats.get("fairness_eval_lane_quota", ""),
                "fairness_eval_lane_candidate_count": wdcg_stats.get("fairness_eval_lane_candidate_count", ""),
                "fairness_eval_lane_evaluated_count": wdcg_stats.get("fairness_eval_lane_evaluated_count", ""),
                "fairness_eval_lane_evaluated_pairs": wdcg_stats.get("fairness_eval_lane_evaluated_pairs", ""),
                "fairness_eval_lane_replacement_diag_count": wdcg_stats.get("fairness_eval_lane_replacement_diag_count", ""),
                "fairness_eval_lane_skipped_already_evaluated_count": wdcg_stats.get("fairness_eval_lane_skipped_already_evaluated_count", ""),
                "fairness_eval_lane_budgeted_out_count": wdcg_stats.get("fairness_eval_lane_budgeted_out_count", ""),
                "fairness_eval_lane_what_if_calls": wdcg_stats.get("fairness_eval_lane_what_if_calls", ""),
                "fairness_eval_lane_replacement_what_if_calls": wdcg_stats.get("fairness_eval_lane_replacement_what_if_calls", ""),
                "fairness_eval_lane_shadowing_revealed_count": wdcg_stats.get("fairness_eval_lane_shadowing_revealed_count", ""),
                "fairness_eval_lane_nonbeneficial_count": wdcg_stats.get("fairness_eval_lane_nonbeneficial_count", ""),
                "target_pair_count": wdcg_stats.get("target_pair_count", ""),
                "target_pair_prequery_coverage_count": wdcg_stats.get("target_pair_prequery_coverage_count", ""),
                "target_pair_postquery_coverage_count": wdcg_stats.get("target_pair_postquery_coverage_count", ""),
                "target_pair_preround_coverage_count": wdcg_stats.get("target_pair_preround_coverage_count", ""),
                "target_pair_postround_coverage_count": wdcg_stats.get("target_pair_postround_coverage_count", ""),
                "target_pair_lane_admitted_count": wdcg_stats.get("target_pair_lane_admitted_count", ""),
                "target_pair_selected_count": wdcg_stats.get("target_pair_selected_count", ""),
                "target_pair_final_count": wdcg_stats.get("target_pair_final_count", ""),
                "target_pair_missing_examples": wdcg_stats.get("target_pair_missing_examples", ""),
                "target_pair_dropped_perquery_examples": wdcg_stats.get("target_pair_dropped_perquery_examples", ""),
                "target_pair_dropped_round_examples": wdcg_stats.get("target_pair_dropped_round_examples", ""),
                "target_pair_fate_summary": wdcg_stats.get("target_pair_fate_summary", ""),
                "mat_pair_key": mat.get("mat_pair_key", ""),
                "mat_pair_in_postround": mat.get("mat_pair_in_postround", ""),
                "mat_pair_in_candidate_conf": mat.get("mat_pair_in_candidate_conf", ""),
                "mat_pair_in_final_conf": mat.get("mat_pair_in_final_conf", ""),
                "mat_pair_evaluated": mat.get("mat_pair_evaluated", ""),
                "mat_pair_main_raw_benefit": mat.get("mat_pair_main_raw_benefit", ""),
                "mat_pair_main_normalized_benefit": mat.get("mat_pair_main_normalized_benefit", ""),
                "mat_pair_main_net_utility": mat.get("mat_pair_main_net_utility", ""),
                "mat_pair_creation_cost": mat.get("mat_pair_creation_cost", ""),
                "mat_replacement_diag_available": mat.get("mat_replacement_diag_available", ""),
                "mat_replacement_net_benefit": mat.get("mat_replacement_net_benefit", ""),
                "mat_replacement_utility": mat.get("mat_replacement_utility", ""),
                "mat_left_prefix": mat.get("mat_left_prefix", ""),
                "mat_left_prefix_in_old_conf": mat.get("mat_left_prefix_in_old_conf", ""),
                "mat_left_prefix_in_candidate_conf": mat.get("mat_left_prefix_in_candidate_conf", ""),
                "mat_left_prefix_in_final_conf": mat.get("mat_left_prefix_in_final_conf", ""),
                "mat_left_prefix_net_utility": mat.get("mat_left_prefix_net_utility", ""),
                "mat_gap_reason": mat.get("mat_gap_reason", ""),
                "materialization_gap_pair_count": wdcg_stats.get("materialization_gap_pair_count", ""),
                "materialization_gap_not_postround_count": wdcg_stats.get("materialization_gap_not_postround_count", ""),
                "materialization_gap_eval_gap_count": wdcg_stats.get("materialization_gap_eval_gap_count", ""),
                "materialization_gap_prefix_shadowing_likely_count": wdcg_stats.get("materialization_gap_prefix_shadowing_likely_count", ""),
                "materialization_gap_replacement_positive_main_nonpositive_count": wdcg_stats.get("materialization_gap_replacement_positive_main_nonpositive_count", ""),
                "materialization_gap_eval_confirmed_nonbeneficial_count": wdcg_stats.get("materialization_gap_eval_confirmed_nonbeneficial_count", ""),
                "materialization_gap_main_positive_but_not_selected_count": wdcg_stats.get("materialization_gap_main_positive_but_not_selected_count", ""),
                "materialization_gap_candidate_conf_rejected_by_beta_count": wdcg_stats.get("materialization_gap_candidate_conf_rejected_by_beta_count", ""),
                "materialization_gap_already_final_count": wdcg_stats.get("materialization_gap_already_final_count", ""),
                "materialization_gap_overlay_applied_count": wdcg_stats.get("materialization_gap_overlay_applied_count", ""),
                "materialization_gap_unknown_count": wdcg_stats.get("materialization_gap_unknown_count", ""),
                "materialization_gap_not_postround_examples": wdcg_stats.get("materialization_gap_not_postround_examples", ""),
                "materialization_gap_eval_gap_examples": wdcg_stats.get("materialization_gap_eval_gap_examples", ""),
                "materialization_gap_prefix_shadowing_examples": wdcg_stats.get("materialization_gap_prefix_shadowing_examples", ""),
                "materialization_gap_replacement_positive_main_nonpositive_examples": wdcg_stats.get("materialization_gap_replacement_positive_main_nonpositive_examples", ""),
                "materialization_gap_eval_confirmed_nonbeneficial_examples": wdcg_stats.get("materialization_gap_eval_confirmed_nonbeneficial_examples", ""),
                "materialization_gap_main_positive_not_selected_examples": wdcg_stats.get("materialization_gap_main_positive_not_selected_examples", ""),
                "replacement_overlay_co_residency_count": wdcg_stats.get("replacement_overlay_co_residency_count", ""),
                "lambda": lam,
                "lambda_shadow": lam_shadow,
                "rsfe": rsfe,
                "mad": mad,
                "ts": ts,
                "decision_ratio": decision_stats.get("ratio", ""),
                "decision_old_benefit": decision_stats.get("old_benefit", ""),
                "decision_new_benefit": decision_stats.get("new_benefit", ""),
            }

            self._writer.writerow(row)
            if self.flush_each_row and self._fh:
                self._fh.flush()
