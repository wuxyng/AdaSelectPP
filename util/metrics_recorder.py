# -*- coding: utf-8 -*-
"""util/metrics_recorder.py (Phase 0.2)

Goals
- Unify per-round metrics schema across LiteSelect/AdaSelect baselines.
- Compute stability/switching signals online (no offline re-processing needed).
- Be robust to caller differences (exec_ms vs exec_s, etc.).

Design
- Writes one CSV row per round + an optional SUMMARY row.
- Flushes each row (configurable) to avoid losing data on timeout/kill.
- Supports context-manager usage: `with MetricsRecorder(...) as r:`

Notes
- Time units: treat inputs as *milliseconds* (ms). Some legacy callers name them
  `*_s` but still pass ms; we do NOT auto-convert.
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Set, Tuple

IndexKey = Tuple[str, Tuple[str, ...]]


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _conf_to_list(conf: Optional[Iterable[IndexKey]]) -> list:
    if not conf:
        return []
    return sorted([(t, tuple(cols)) for (t, cols) in conf], key=lambda x: (x[0], x[1]))


@dataclass
class _Cumulative:
    diff_sum: float = 0.0
    size_sum: float = 0.0


class MetricsRecorder:
    """Write per-round metrics and compute stability signals online."""

    # Keep core columns compatible with Phase 0.1/0.2 post-processing.
    FIELD_LIST: Sequence[str] = (
        "round",
        "exec",
        "rec",
        "trans",
        "total",
        # duplicate names used in some roadmap drafts
        "g_exe",
        "g_rec",
        "g_trans",
        "g_total",
        # A-metrics
        "what_if_calls",
        "candidate_count",
        "evaluated_count",
        "replacement_probe_count",
        "replacement_what_if_calls",
        "replacement_hit_count",
        "replacement_ok_count",
        "replacement_fail_count",
        "replacement_diag_time",
        "preconf_count",
        "filtered_nonpositive_count",
        "candidate_count_raw",
        "wdcg_pruned_count",
        "wdcg_selected_post_compile",
        # G0-3 / Phase 0.5 merge+compile signals
        "merged_total",
        "merged_group",
        "merged_order",
        "merged_covering",
        "compile_validation_enabled",
        "compile_validation_passes",
        "compile_validation_trials",
        "compile_validated",
        "compile_invalidated",
        "compile_errors",
        "compile_not_picked",
        # Phase 0.5: table/DML-aware pruning signals (WDCG funnel)
        "pruned_small_tables",
        "skipped_high_dml_tables",
        "dml_tables_downweighted",
        "dml_weight_min",
        "dml_weight_max",
        "coverage_ratio",
        "wdcg_elapsed_ms",
        # Clean candidate generator diagnostics
        "parse_ast_ok",
        "parse_fallback_regex",
        "gen_mode",
        "probe_rounds",
        "workload_count",
        "width1_count",
        "width2_count",
        "seed_count",
        "eligible_seed_count",
        "multi_growth_count",
        "rejected_growth_has_or",
        "rejected_growth_alias_ambiguous",
        "rejected_growth_seed_not_positive",
        "rejected_growth_seed_unseen",
        "rejected_growth_range_seed",
        "rejected_growth_parse_fallback",
        "family_eq1",
        "family_join_eq1",
        "family_range1",
        "family_eqeq",
        "family_eqrange",
        "family_rescue",
        "source_ast",
        "source_strong_ast",
        "source_static_fallback",
        "source_vacuum_rescue",
        "vocab_enabled",
        "vocab_tables",
        "vocab_columns",
        "wdcg_skipped_family",
        "wdcg_skipped_dominated",
        "coverage_boost_added",
        "wdcg_warmup_active",
        "structural_pair_quota",
        "structural_pair_eval_count",
        "structural_pair_eval_selected_keys",
        "structural_pair_eval_budgeted_out_count",
        "structural_pair_eval_lane_enabled",
        # Diagnostics: per-index query impact (aff) statistics
        "aff_avg",
        "aff_p90",
        "aff_max",
        "aff_suspicious_frac",
        "predicted_what_if_calls",
        "aff_top",
        "reconf_add",
        "reconf_drop",
        "trans_create",
        "trans_drop",
        # stability: dead-zone support gate (AdaSelect)
        "deadzone_old_support",
        "deadzone_blocked",
        "decision_ratio",
        "decision_old_benefit",
        "decision_new_benefit",
        # WDCG correctness diagnostics
        "corr_trials",
        "old_relevant_count",
        "old_relevant_not_appearing_count",
        # stability
        "switch_size",
        "oscillation_rate",
        "stability_score",
        # uncertainty & regime flags (Phase 1+; may be empty in 0.2)
        "avg_sigma",
        "sigma_epi",
        "sigma_drift",
        "freeze_flag",
        "drift_flag",
        "regime",
        # knobs
        "alpha_ema",
        "lambda_ctrl",
        "beta",
        "switched",
        # configurations
        "old",
        "new",
        # robustness
        "timeout",
        "exit_code",
        "message",
    )

    def __init__(self, csv_path: str, osc_window: int = 20, flush_each_row: bool = True) -> None:
        self.csv_path = Path(csv_path)
        _ensure_parent(self.csv_path)

        self.osc_window = int(max(1, osc_window))
        self.flush_each_row = bool(flush_each_row)

        self._toggle_hist: deque[Set[str]] = deque(maxlen=self.osc_window)
        self._cum = _Cumulative()
        self._prev_conf: Optional[Set[str]] = None

        self._fp = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fp, fieldnames=list(self.FIELD_LIST))
        self._writer.writeheader()
        self._flush()

    # --- context manager ---
    def __enter__(self) -> "MetricsRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._flush()
        except Exception:
            pass
        try:
            self._fp.close()
        except Exception:
            pass

    def _flush(self) -> None:
        try:
            self._fp.flush()
            try:
                os.fsync(self._fp.fileno())
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _conf_to_repr_set(conf: Optional[Iterable[IndexKey]]) -> Set[str]:
        return {repr((t, tuple(cols))) for (t, cols) in conf} if conf else set()

    def _update_stability(self, new_conf_repr: Set[str]) -> Tuple[int, float, float]:
        """Return (switch_size, oscillation_rate_W, stability_score)."""
        if self._prev_conf is None:
            delta_set: Set[str] = set()
            switch_size = 0
        else:
            delta_set = self._prev_conf.symmetric_difference(new_conf_repr)
            switch_size = len(delta_set)

        # update oscillation window
        self._toggle_hist.append(delta_set)

        # strict Phase-0.2 style oscillation rate
        toggle_counts: Dict[str, int] = defaultdict(int)
        total_switches = 0
        for s in self._toggle_hist:
            total_switches += len(s)
            for idx in s:
                toggle_counts[idx] += 1
        if total_switches == 0:
            osc_rate = 0.0
        else:
            repeats = sum(max(0, c - 1) for c in toggle_counts.values())
            osc_rate = repeats / float(total_switches)

        if self._prev_conf is not None:
            self._cum.diff_sum += float(len(delta_set))
        self._cum.size_sum += float(len(new_conf_repr))
        stability = 1.0 - (self._cum.diff_sum / (self._cum.size_sum + 1e-9))

        self._prev_conf = set(new_conf_repr)
        return switch_size, osc_rate, stability

    @staticmethod
    def _pick_time_ms(fields: Dict[str, Any], base: str) -> Optional[float]:
        """Pick time from possible aliases: {base}_ms, {base}_s, base."""
        for k in (f"{base}_ms", f"{base}_s", base):
            if k in fields and fields[k] is not None:
                try:
                    return float(fields[k])
                except Exception:
                    return None
        return None

    def record_round(self, *, round_id: int, old_conf: Optional[Iterable[IndexKey]] = None,
                     new_conf: Optional[Iterable[IndexKey]] = None, switched: int = 0,
                     **fields: Any) -> None:
        """Write one round.

        Required: round_id.
        Times: pass exec_ms/rec_ms/trans_ms/total_ms OR exec_s/rec_s/... (treated as ms).
        Other fields: any keys in FIELD_LIST are accepted; unknown keys are ignored.
        """

        exec_ms = self._pick_time_ms(fields, "exec")
        rec_ms = self._pick_time_ms(fields, "rec")
        trans_ms = self._pick_time_ms(fields, "trans")
        total_ms = self._pick_time_ms(fields, "total")

        # fallback: allow g_* aliases too
        if exec_ms is None and fields.get("g_exe") is not None:
            exec_ms = float(fields["g_exe"])
        if rec_ms is None and fields.get("g_rec") is not None:
            rec_ms = float(fields["g_rec"])
        if trans_ms is None and fields.get("g_trans") is not None:
            trans_ms = float(fields["g_trans"])
        if total_ms is None and fields.get("g_total") is not None:
            total_ms = float(fields["g_total"])

        exec_ms = float(exec_ms or 0.0)
        rec_ms = float(rec_ms or 0.0)
        trans_ms = float(trans_ms or 0.0)
        total_ms = float(total_ms or (exec_ms + rec_ms + trans_ms))

        new_repr = self._conf_to_repr_set(new_conf)
        switch_size, osc_rate, stability = self._update_stability(new_repr)

        row: Dict[str, Any] = {k: "" for k in self.FIELD_LIST}
        row.update(
            {
                "round": int(round_id),
                "exec": exec_ms,
                "rec": rec_ms,
                "trans": trans_ms,
                "total": total_ms,
                "g_exe": exec_ms,
                "g_rec": rec_ms,
                "g_trans": trans_ms,
                "g_total": total_ms,
                "switch_size": int(switch_size),
                "oscillation_rate": float(osc_rate),
                "stability_score": float(stability),
                "switched": int(switched),
                "old": repr(_conf_to_list(old_conf)),
                "new": repr(_conf_to_list(new_conf)),
            }
        )

        # fill any additional known keys
        for k, v in list(fields.items()):
            if k in row and v is not None:
                row[k] = v

        self._writer.writerow(row)
        if self.flush_each_row:
            self._flush()

    def write_summary(self, **fields: Any) -> None:
        """Write a final SUMMARY row (optional)."""
        row: Dict[str, Any] = {k: "" for k in self.FIELD_LIST}
        row["round"] = "SUMMARY"

        # common alias mapping
        alias = {
            "exec_sum": "exec",
            "rec_sum": "rec",
            "trans_sum": "trans",
            "total_sum": "total",
            "whatif_sum": "what_if_calls",
            "what_if_total": "what_if_calls",
        }
        for k, v in fields.items():
            kk = alias.get(k, k)
            if kk in row and v is not None:
                row[kk] = v

        self._writer.writerow(row)
        if self.flush_each_row:
            self._flush()
