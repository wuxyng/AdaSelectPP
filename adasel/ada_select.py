# -*- coding: utf-8 -*-
"""AdaSelect++ bounded prefix-growth spine.

This implementation keeps the stable LiteSelect-style benefit estimation,
logging, timeout reset, and top-k/beta transition policy, but does NOT use
LiteSelect's exhaustive permutation candidate enumeration.

Candidate generation is delegated to MCIGCandidateGenerator:
  - static SQL predicate/join/range evidence only;
  - single-column seeds;
  - bounded width-2 prefix growth;
  - per-query/per-table caps;
  - no CooccurrenceEnumerator, no G0-3 merge, no compile hard gate, no EXPLAIN-plan
    candidate generation.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from adaselect_pp.common import sql_only
from util.benefit_normalizer import BenefitNormalizer
from adaselect_pp.candidate_gen_v2 import MCIGCandidateGenerator
from adasel.config_flags import coerce_bool_flag, parse_target_pair_audit

logger = logging.getLogger(__name__)

IndexKey = Tuple[str, Tuple[str, ...]]
DEFAULT_COST = 1.0


def _unique_preserve_order(cols: Sequence[str]) -> Tuple[str, ...]:
    seen: Set[str] = set()
    ordered: List[str] = []
    for c in cols:
        cc = str(c).strip().lower()
        if not cc or cc in seen:
            continue
        seen.add(cc)
        ordered.append(cc)
    return tuple(ordered)


def _canon(key: Tuple[Any, ...]) -> IndexKey:
    if len(key) >= 2 and not isinstance(key[1], tuple):
        return (str(key[0]).lower(), tuple(str(c).lower() for c in key[1:]))
    return (str(key[0]).lower(), tuple(str(c).lower() for c in key[1]))


def _merge_prefixes(idxs: Set[IndexKey]) -> Set[IndexKey]:
    """Keep widest per-prefix per table to reduce redundant candidate pool.

    This is copied from the proven LiteSelectMC path.  If (a,b) exists, (a)
    is removed.  It does not invent candidates; it only reduces prefix
    redundancy after exhaustive enumeration.
    """
    by_table: Dict[str, List[IndexKey]] = {}
    for tbl, cols in idxs:
        by_table.setdefault(tbl, []).append((tbl, tuple(cols)))

    kept: Set[IndexKey] = set()
    for tbl, entries in by_table.items():
        entries.sort(key=lambda x: (len(x[1]), x[1]), reverse=True)
        for ent in entries:
            _t, cols = ent
            if not any(cols == big[1][: len(cols)] for big in kept if big[0] == tbl):
                kept.add(ent)
    return kept


class AdaSelect:
    """AdaSelect spine with bounded predicate-first prefix-growth candidates."""

    def __init__(self, benchmark: str, cost_eval, db_con1, db_con2, cfg_path: str = "adasel/config/adaselect.json", cfg_source: Any = None) -> None:
        self.benchmark = benchmark
        self.cost_eval = cost_eval
        self.db_con1 = db_con1
        self.db_con2 = db_con2

        # Minimal effective knobs.
        self.max_num = 10
        self.alpha_init = 0.65
        self.beta = 1.10
        self.ratio = 0.50
        self.timeout = 30_000
        self.transition_mode = "symmetric"
        self.min_width = 1
        self.max_width = 2
        self.rsfe_decay = 0.90
        self.lambda_policy = "adaptive"
        self.benefit_decay = None
        self.benefit_decay_fixed = 0.95
        # AdaSelect adaptive smoothing knobs.  These are core AdaSelect
        # benefit-update parameters, not candidate-generation switches.
        self.fixed_lambda = self.alpha_init
        self.beta_error = 0.20
        self.lambda_min = 0.20
        self.lambda_max = 0.95
        self.ts_low = 0.50
        self.ts_high = 2.00
        self.ts_gate_regress = 0.05
        self.ts_mad_floor_rel = 1e-6
        self.ts_sign_decay = 0.90
        self.wdcg_enabled = True
        self.replacement_overlay_enabled = False
        self.pair_supply_ceiling_enabled = False
        self.target_pair_audit: Set[IndexKey] = set()
        self.log_candidate_sample = 12
        self.candidate_topk_factor = 4
        self.candidate_topk_min_extra = 6
        self.candidate_per_query_cap = 12
        self.candidate_per_table_cap = 4
        self.candidate_round_table_cap = 6
        self.indexable_columns_path = ""
        self._cfg_effective: Dict[str, Any] = {}
        self._load_cfg(cfg_source if cfg_source is not None else cfg_path)
        if self.max_width > 2:
            raise ValueError("Phase 0.5 AdaSelect-PG supports max_width <= 2 only")
        if not self.wdcg_enabled:
            raise ValueError(
                "wdcg_enabled=false is not supported: Phase 0.5 has only the MCIGCandidateGenerator active path"
            )

        logger.info(
            "cfg: K=%d α=%.2f β=%.2f ratio=%.2f timeout=%d mode=%s min_w=%d max_w=%d",
            self.max_num,
            self.alpha_init,
            self.beta,
            self.ratio,
            self.timeout,
            self.transition_mode,
            self.min_width,
            self.max_width,
        )
        logger.info("GitInfo | %s", self._git_info())
        logger.info("ConfigDump | %s", json.dumps(self._cfg_effective, sort_keys=True))

        # Schema + bounded prefix-growth candidate generator.
        self.tables = [str(t).lower() for t in self.db_con1.get_tables()]
        self._existing_indexes: Dict[str, Set[IndexKey]] = {}
        self._cache_indexes()
        self.candidate_generator = MCIGCandidateGenerator(
            benchmark=self.benchmark,
            db_con=self.db_con1,
            max_width=self.max_width,
            max_num=max(1, self.max_num * self.candidate_topk_factor + self.candidate_topk_min_extra),
            indexable_path=self.indexable_columns_path,
            per_query_cap=self.candidate_per_query_cap,
            per_table_cap=self.candidate_per_table_cap,
            round_table_cap=self.candidate_round_table_cap,
        )
        self._wdcg_gen = self.candidate_generator

        # Creation cost model.
        self.benefit_norm = BenefitNormalizer()
        try:
            self.benefit_norm.load_creation_costs(
                benchmark,
                required=True,
                db_con=self.db_con1,
                vocabulary=getattr(self.candidate_generator, "vocab", None),
            )
        except Exception as exc:
            logger.error("creation-cost load failed for benchmark=%s: %s", benchmark, exc)
            raise
        logger.info(
            "CreationCostDump | path=%s status=%s parsed_entries=%d raw_entries=%d table_entries=%d collisions=%d unresolved=%d",
            self.benefit_norm.creation_cost_path,
            self.benefit_norm.creation_cost_status,
            self.benefit_norm.creation_cost_entries,
            self.benefit_norm.creation_cost_raw_entries,
            len(getattr(self.benefit_norm, "index_costs_by_key", {})),
            len(getattr(self.benefit_norm, "creation_cost_collisions", {})),
            len(getattr(self.benefit_norm, "creation_cost_unresolved", set())),
        )

        # State.
        self.columns_benefit: Dict[IndexKey, float] = {}
        self.workload_count: int = 0
        self.consecutive_timeouts: int = 0
        self.last_stable_config: Set[IndexKey] = set()

        # Optional adaptive state kept for TraceRecorder compatibility.
        self.idx_alphas: Dict[IndexKey, float] = {}
        self.idx_alphas_shadow: Dict[IndexKey, float] = {}
        self.idx_error_smooth: Dict[IndexKey, float] = {}
        self.idx_abs_error_smooth: Dict[IndexKey, float] = {}
        self.idx_seen_cnt: Dict[IndexKey, int] = {}
        self.idx_positive_cnt: Dict[IndexKey, int] = {}
        self.idx_first_seen_round: Dict[IndexKey, int] = {}
        self.idx_last_seen_round: Dict[IndexKey, int] = {}
        self.idx_seen_rounds: Dict[IndexKey, Set[int]] = {}
        self.idx_last_err_sign: Dict[IndexKey, int] = {}
        self.idx_sign_smooth: Dict[IndexKey, float] = {}
        self.idx_last_obs_src: Dict[IndexKey, str] = {}

        # Per-round diagnostics expected by main.py / trace recorder.
        self._m_stats: Dict[str, float] = {
            "what_if_calls": 0,
            "candidate_count": 0,
            "evaluated_count": 0,
            "replacement_probe_count": 0,
            "replacement_what_if_calls": 0,
            "replacement_hit_count": 0,
            "replacement_ok_count": 0,
            "replacement_fail_count": 0,
            "replacement_diag_time": 0.0,
            "reconf_add": 0,
            "reconf_drop": 0,
            "trans_create": 0.0,
            "trans_drop": 0.0,
        }
        self._last_base_total = 0.0
        self._last_evaluated_set: Set[IndexKey] = set()
        self._last_eval_order: List[IndexKey] = []
        self._last_appearing_set: Set[IndexKey] = set()
        self._last_candidate_conf: Set[IndexKey] = set()
        self._last_final_conf: Set[IndexKey] = set()
        self._last_net_benefit_map: Dict[IndexKey, float] = {}
        self._last_obs_delta_map: Dict[IndexKey, float] = {}
        self._last_obs_src_map: Dict[IndexKey, str] = {}
        self._last_decision_stats: Dict[str, float] = {}
        self._last_wdcg_score_map: Dict[IndexKey, float] = {}
        self._last_wdcg_stats: Dict[str, Any] = {}
        self._last_structural_pair_replacement_map: Dict[IndexKey, Dict[str, Any]] = {}
        self._last_structural_pair_candidate_set: Set[IndexKey] = set()
        self._last_structural_pair_lane_set: Set[IndexKey] = set()
        self._last_pair_fate_map: Dict[IndexKey, str] = {}
        self._last_overlay_opportunity_pairs: Set[IndexKey] = set()
        self._last_overlay_admitted_pairs: Set[IndexKey] = set()
        self._last_overlay_fired_pairs: Set[IndexKey] = set()
        self._last_shadow_action_rows: List[Dict[str, Any]] = []
        self._last_deadzone_stats: Dict[str, Any] = {"deadzone_old_support": 0, "deadzone_blocked": 0}

    # ------------------------------------------------------------------
    # Configuration / initialization
    # ------------------------------------------------------------------
    def _load_cfg(self, cfg_source: Any) -> None:
        if isinstance(cfg_source, dict):
            cfg = dict(cfg_source)
        else:
            try:
                with open(str(cfg_source), encoding="utf-8") as f:
                    cfg = json.load(f) or {}
            except Exception:
                cfg = {}
        self.max_num = int(cfg.get("max_num", self.max_num))
        self.alpha_init = float(cfg.get("alpha", self.alpha_init))
        self.beta = float(cfg.get("beta", self.beta))
        self.ratio = float(cfg.get("optimizer_ratio", cfg.get("ratio", self.ratio)))
        self.timeout = int(cfg.get("timeout", self.timeout))
        self.min_width = int(cfg.get("min_width", cfg.get("min_index_width", self.min_width)))
        self.max_width = int(cfg.get("max_width", cfg.get("max_index_width", self.max_width)))
        self.transition_mode = str(cfg.get("transition_mode", self.transition_mode))
        self.rsfe_decay = float(cfg.get("rsfe_decay", self.rsfe_decay))
        self.lambda_policy = str(cfg.get("lambda_policy", self.lambda_policy)).lower()
        self.fixed_lambda = float(cfg.get("fixed_lambda", cfg.get("alpha", self.alpha_init)))
        _bd = cfg.get("benefit_decay", self.benefit_decay)
        self.benefit_decay = None if _bd is None else float(_bd)
        self.benefit_decay_fixed = float(cfg.get("benefit_decay_fixed", self.benefit_decay_fixed))
        self.beta_error = float(cfg.get("beta_error", self.beta_error))
        self.lambda_min = float(cfg.get("lambda_min", self.lambda_min))
        self.lambda_max = float(cfg.get("lambda_max", self.lambda_max))
        self.ts_low = float(cfg.get("ts_low", self.ts_low))
        self.ts_high = float(cfg.get("ts_high", self.ts_high))
        self.ts_gate_regress = float(cfg.get("ts_gate_regress", self.ts_gate_regress))
        self.ts_mad_floor_rel = float(cfg.get("ts_mad_floor_rel", self.ts_mad_floor_rel))
        self.ts_sign_decay = float(cfg.get("ts_sign_decay", self.ts_sign_decay))
        if self.lambda_min > self.lambda_max:
            self.lambda_min, self.lambda_max = self.lambda_max, self.lambda_min
        self.wdcg_enabled = bool(cfg.get("wdcg_enabled", self.wdcg_enabled))
        replacement_overlay_cfg = cfg.get("replacement_overlay_enabled", self.replacement_overlay_enabled)
        if isinstance(replacement_overlay_cfg, str):
            self.replacement_overlay_enabled = replacement_overlay_cfg.strip().lower() in ("1", "true", "yes", "on")
        else:
            self.replacement_overlay_enabled = bool(replacement_overlay_cfg)
        self.pair_supply_ceiling_enabled = coerce_bool_flag(
            cfg.get("pair_supply_ceiling_enabled", self.pair_supply_ceiling_enabled)
        )
        self.target_pair_audit = parse_target_pair_audit(cfg.get("target_pair_audit", ""))
        self.log_candidate_sample = int(cfg.get("log_candidate_sample", self.log_candidate_sample))
        self.candidate_topk_factor = int(cfg.get("candidate_topk_factor", self.candidate_topk_factor))
        self.candidate_topk_min_extra = int(cfg.get("candidate_topk_min_extra", self.candidate_topk_min_extra))
        self.candidate_per_query_cap = int(cfg.get("candidate_per_query_cap", self.candidate_per_query_cap))
        self.candidate_per_table_cap = int(cfg.get("candidate_per_table_cap", self.candidate_per_table_cap))
        self.candidate_round_table_cap = int(cfg.get("candidate_round_table_cap", self.candidate_round_table_cap))
        self.indexable_columns_path = str(cfg.get("indexable_columns_path", cfg.get("g0_indexable_columns_path", self.indexable_columns_path)) or "")
        self._cfg_effective = {
            "max_num": self.max_num,
            "alpha": self.alpha_init,
            "beta": self.beta,
            "optimizer_ratio": self.ratio,
            "timeout": self.timeout,
            "min_width": self.min_width,
            "max_width": self.max_width,
            "transition_mode": self.transition_mode,
            "rsfe_decay": self.rsfe_decay,
            "lambda_policy": self.lambda_policy,
            "wdcg_enabled": self.wdcg_enabled,
            "replacement_overlay_enabled": self.replacement_overlay_enabled,
            "pair_supply_ceiling_enabled": self.pair_supply_ceiling_enabled,
            "target_pair_audit": self._fmt_config(self.target_pair_audit),
            "benefit_decay_fixed": self.benefit_decay_fixed,
            "candidate_topk_factor": self.candidate_topk_factor,
            "candidate_topk_min_extra": self.candidate_topk_min_extra,
            "candidate_per_query_cap": self.candidate_per_query_cap,
            "candidate_per_table_cap": self.candidate_per_table_cap,
            "candidate_round_table_cap": self.candidate_round_table_cap,
            "indexable_columns_path": self.indexable_columns_path,
            "log_candidate_sample": self.log_candidate_sample,
            "fixed_lambda": self.fixed_lambda,
            "benefit_decay": self.benefit_decay,
            "beta_error": self.beta_error,
            "lambda_min": self.lambda_min,
            "lambda_max": self.lambda_max,
            "ts_low": self.ts_low,
            "ts_high": self.ts_high,
            "ts_gate_regress": self.ts_gate_regress,
            "ts_mad_floor_rel": self.ts_mad_floor_rel,
            "ts_sign_decay": self.ts_sign_decay,
        }

    @staticmethod
    def _git_info() -> Dict[str, Any]:
        def run_git(args: Sequence[str]) -> str:
            try:
                proc = subprocess.run(
                    ["git", *args],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                return proc.stdout.strip() if proc.returncode == 0 else ""
            except Exception:
                return ""

        return {
            "branch": run_git(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown",
            "sha": run_git(["rev-parse", "--short", "HEAD"]) or "unknown",
            "dirty": bool(run_git(["status", "--short"])),
        }

    def _cache_indexes(self) -> None:
        """Cache existing PRIMARY KEY / UNIQUE indexes to avoid re-creating them."""
        for tbl in self.tables:
            idxs: Set[IndexKey] = set()
            try:
                defs = self.db_con1.exec_fetchall(f"SELECT indexdef FROM pg_indexes WHERE tablename = '{tbl}'")
                for (idxdef,) in defs:
                    if " UNIQUE " in idxdef or "PRIMARY KEY" in idxdef:
                        m = re.search(r"\(([^)]+)\)", idxdef)
                        if m:
                            cols = tuple(c.strip().strip('"').lower() for c in m.group(1).split(','))
                            if cols:
                                idxs.add((tbl.lower(), cols))
            except Exception as exc:
                logger.warning("PK/UNIQUE cache failed table=%s: %s", tbl, exc)
            self._existing_indexes[tbl.lower()] = idxs
        logger.debug("Cached PK/UNIQUE: %s", self._existing_indexes)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _minmax_norm(data: Dict[IndexKey, float]) -> Dict[IndexKey, float]:
        """Legacy robust max-scale normalization retained for compatibility."""
        if not data:
            return {}
        vals = list(data.values())
        positive_vals = [v for v in vals if v > 1e-9]
        scale_factor = 1.0
        if positive_vals:
            sorted_pos = sorted(positive_vals)
            p95_index = min(int(len(sorted_pos) * 0.95), len(sorted_pos) - 1)
            p95_val = sorted_pos[p95_index]
            if p95_val > 1e-9:
                scale_factor = p95_val
        normalized: Dict[IndexKey, float] = {}
        for k, v in data.items():
            nv = float(v) / scale_factor
            if nv > 1.0:
                nv = 1.0
            if nv < -2.0:
                nv = -2.0
            normalized[k] = nv
        return normalized

    @staticmethod
    def _log_positive_norm(data: Dict[IndexKey, float]) -> Dict[IndexKey, float]:
        """Normalize only positive benefit with log1p so one outlier does not flatten the field."""
        if not data:
            return {}
        positives: Dict[IndexKey, float] = {k: max(0.0, float(v)) for k, v in data.items()}
        scale = max(positives.values()) if positives else 0.0
        if scale <= 0.0:
            return {k: 0.0 for k in data}
        denom = math.log1p(scale)
        return {k: math.log1p(v) / denom for k, v in positives.items()}

    @staticmethod
    def _log_positive_norm_value(value: float, scale: float) -> float:
        if float(scale) <= 0.0:
            return 0.0
        return math.log1p(max(0.0, float(value))) / math.log1p(float(scale))

    def _creation_cost(self, key: IndexKey) -> float:
        if hasattr(self.benefit_norm, "creation_cost_for"):
            return float(self.benefit_norm.creation_cost_for(key[0], tuple(key[1]), DEFAULT_COST))
        return float(self.benefit_norm.index_costs.get(tuple(key[1]), DEFAULT_COST))

    def _reset_round_diagnostics(self) -> None:
        self._last_evaluated_set = set()
        self._last_eval_order = []
        self._last_appearing_set = set()
        self._last_candidate_conf = set()
        self._last_final_conf = set()
        self._last_net_benefit_map = {}
        self._last_obs_delta_map = {}
        self._last_obs_src_map = {}
        self._last_decision_stats = {}
        self._last_wdcg_score_map = {}
        self._last_wdcg_stats = {}
        self._last_structural_pair_replacement_map = {}
        self._last_structural_pair_candidate_set = set()
        self._last_structural_pair_lane_set = set()
        self._last_pair_fate_map = {}
        self._last_overlay_opportunity_pairs = set()
        self._last_overlay_admitted_pairs = set()
        self._last_overlay_fired_pairs = set()
        self._last_shadow_action_rows = []
        self._last_deadzone_stats = {"deadzone_old_support": 0, "deadzone_blocked": 0}

    @staticmethod
    def _fmt_index_key(key: IndexKey) -> str:
        return f"{key[0]}({','.join(key[1])})"

    @classmethod
    def _fmt_config(cls, conf: Set[IndexKey]) -> str:
        """Serialize configs with semicolon delimiters; columns already use commas."""
        return ";".join(cls._fmt_index_key(k) for k in sorted(set(conf or set())))

    @classmethod
    def _fmt_actions(cls, actions: List[Dict[str, Any]]) -> str:
        return "|".join(str(a.get("action_key", "")) for a in actions if str(a.get("action_key", "")))

    def _candidate_meta_map(self) -> Dict[IndexKey, Dict[str, Any]]:
        try:
            _gen = getattr(self, "_wdcg_gen", None)
            meta = getattr(getattr(_gen, "enum", None), "last_meta", None)
            if isinstance(meta, dict):
                return meta
        except Exception:
            pass
        return {}

    def _structural_pair_type(self, key: IndexKey, meta_map: Optional[Dict[IndexKey, Dict[str, Any]]] = None) -> str:
        if len(key[1]) != 2:
            return ""
        meta_map = meta_map if isinstance(meta_map, dict) else self._candidate_meta_map()
        meta = meta_map.get(key, {}) if isinstance(meta_map, dict) else {}
        family = str(meta.get("family", "") or "") if isinstance(meta, dict) else ""
        explicit_type = str(meta.get("structural_pair_type", "") or "") if isinstance(meta, dict) else ""
        if explicit_type:
            return explicit_type
        seed_key = meta.get("seed_key", None) if isinstance(meta, dict) else None
        seed_family = ""
        if isinstance(seed_key, tuple) and len(seed_key) == 2 and isinstance(seed_key[1], tuple):
            seed_meta = meta_map.get(seed_key, {}) if isinstance(meta_map, dict) else {}
            if isinstance(seed_meta, dict):
                seed_family = str(seed_meta.get("family", "") or "")
        if family == "EQ_RANGE" and seed_family == "JOIN_EQ1":
            return "JOIN_RANGE"
        if family == "EQ_EQ" and seed_family == "JOIN_EQ1":
            return "JOIN_EQ"
        return family

    def _diagnostic_structural_pair_type(self, key: IndexKey, meta_map: Optional[Dict[IndexKey, Dict[str, Any]]] = None) -> str:
        if len(key[1]) != 2:
            return ""
        meta_map = meta_map if isinstance(meta_map, dict) else self._candidate_meta_map()
        meta = meta_map.get(key, {}) if isinstance(meta_map, dict) else {}
        family = str(meta.get("family", "") or "") if isinstance(meta, dict) else ""
        seed_family = str(meta.get("grow_seed_family", "") or "") if isinstance(meta, dict) else ""
        if family == "EQ_RANGE" and seed_family == "JOIN_EQ1":
            return "JOIN_RANGE"
        if family == "EQ_EQ" and seed_family == "JOIN_EQ1":
            return "JOIN_EQ"
        return self._structural_pair_type(key, meta_map)

    def _is_structural_pair_candidate(
        self,
        key: IndexKey,
        old_conf: Set[IndexKey],
        meta_map: Optional[Dict[IndexKey, Dict[str, Any]]] = None,
    ) -> bool:
        if len(key[1]) != 2 or key in old_conf:
            return False
        meta_map = meta_map if isinstance(meta_map, dict) else self._candidate_meta_map()
        meta = meta_map.get(key, {}) if isinstance(meta_map, dict) else {}
        family = str(meta.get("family", "") or "") if isinstance(meta, dict) else ""
        if family and family not in {"EQ_RANGE", "EQ_EQ", "JOIN_RANGE", "JOIN_EQ"}:
            return False
        grow_reason = str(meta.get("grow_reason", "") or "") if isinstance(meta, dict) else ""
        pair_type = self._structural_pair_type(key, meta_map)
        return (
            pair_type in {"JOIN_RANGE", "EQ_RANGE", "JOIN_EQ", "EQ_EQ"}
            or grow_reason in {"seed_eq_plus_range", "seed_eq_plus_eq", "JOIN_RANGE", "JOIN_EQ"}
            or family in {"EQ_RANGE", "EQ_EQ"}
        )

    def _rank_structural_pair_candidates(
        self,
        candidates: Sequence[IndexKey],
        meta_map: Optional[Dict[IndexKey, Dict[str, Any]]] = None,
    ) -> List[IndexKey]:
        meta_map = meta_map if isinstance(meta_map, dict) else self._candidate_meta_map()
        priority = {"JOIN_RANGE": 0, "EQ_RANGE": 1, "JOIN_EQ": 2, "EQ_EQ": 3}

        def _as_float(value: Any, default: float = 0.0) -> float:
            try:
                return float(value)
            except Exception:
                return default

        def _sort_key(key: IndexKey) -> Tuple[int, float, float, float, IndexKey]:
            meta = meta_map.get(key, {}) if isinstance(meta_map, dict) else {}
            pair_type = self._structural_pair_type(key, meta_map)
            seed_norm = _as_float(meta.get("seed_normalized_benefit", 0.0) if isinstance(meta, dict) else 0.0)
            score = _as_float(self._last_wdcg_score_map.get(key, meta.get("score", 0.0) if isinstance(meta, dict) else 0.0))
            return (priority.get(pair_type, 99), -seed_norm, -score, self._creation_cost(key), key)

        return sorted(candidates, key=_sort_key)

    @staticmethod
    def _structural_pair_replacement_context(pair: IndexKey, old_conf: Set[IndexKey]) -> Dict[str, Any]:
        table, cols = pair
        if len(cols) != 2:
            return {
                "left_prefix_single": None,
                "component_singles": tuple(),
                "replacement_conf": set(old_conf or set()),
            }
        left_prefix = (table, (cols[0],))
        component_singles = (left_prefix, (table, (cols[1],)))
        replacement_conf = set(old_conf or set())
        replacement_conf.discard(left_prefix)
        replacement_conf.add(pair)
        return {
            "left_prefix_single": left_prefix,
            "component_singles": component_singles,
            "replacement_conf": replacement_conf,
        }

    def _bump_replacement_metric(self, name: str, value: float = 1.0) -> None:
        try:
            self._m_stats[name] = self._m_stats.get(name, 0.0) + value
        except Exception:
            pass
        try:
            self._last_wdcg_stats[name] = self._last_wdcg_stats.get(name, 0.0) + value
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Core flow
    # ------------------------------------------------------------------
    def _initial_costs(self, workload: List[str]) -> Tuple[List[float], float]:
        costs = [float(self.cost_eval.calculate_now_cost([q])) for q in workload]
        return costs, float(sum(costs))

    def _generate_and_merge_candidates(self, workload: List[str], old_conf: Optional[Set[IndexKey]] = None) -> Tuple[List[Set[IndexKey]], Set[IndexKey]]:
        """Generate a bounded, predicate-first candidate pool.

        This intentionally does NOT enumerate all permutations.  The generator
        uses MCIGCandidateGenerator, whose candidates are static SQL evidence
        based: single-column seeds plus bounded width-2 prefix growth.
        """
        topk = max(1, self.max_num * self.candidate_topk_factor + self.candidate_topk_min_extra)
        seed_norm = self._log_positive_norm(self.columns_benefit)
        res = self.candidate_generator.generate(
            workload,
            old_conf=set(old_conf or set()),
            topk=topk,
            workload_count=self.workload_count,
            seed_benefit=self.columns_benefit,
            seed_seen_count=self.idx_seen_cnt,
            seed_positive_count=self.idx_positive_cnt,
            seed_last_obs_src=self.idx_last_obs_src,
            seed_first_seen_round=self.idx_first_seen_round,
            seed_last_seen_round=self.idx_last_seen_round,
            seed_seen_rounds=self.idx_seen_rounds,
            seed_normalized_benefit=seed_norm,
            pair_supply_ceiling_enabled=self.pair_supply_ceiling_enabled,
            target_pair_audit=set(self.target_pair_audit),
        )
        query_indexes = [set(x) for x in (res.query_indexes or [])]
        appearing = set(res.topk_set or set())

        for idx in appearing:
            self.columns_benefit.setdefault(idx, 0.0)
            if len(idx[1]) == 1:
                self.idx_first_seen_round.setdefault(idx, self.workload_count)
                self.idx_last_seen_round[idx] = self.workload_count
                self.idx_seen_rounds.setdefault(idx, set()).add(self.workload_count)

        self._last_wdcg_score_map = dict(res.score_map or {})
        self._last_wdcg_stats = dict(res.stats or {})
        # TraceRecorder compatibility.
        self._wdcg_gen = self.candidate_generator

        raw_sum = int(self._last_wdcg_stats.get("candidate_count_raw", len(set().union(*query_indexes)) if query_indexes else 0))
        raw_counts = [len(qs) for qs in query_indexes]
        sample = sorted(appearing)[: self.log_candidate_sample]
        logger.info(
            "CandidateGen | mode=%s raw_union=%d appearing=%d per_query=%s sample=%s families=%s "
            "width1=%s width2=%s seed_count=%s eligible_seed_count=%s multi_growth=%s parse_ast_ok=%s parse_regex=%s",
            self._last_wdcg_stats.get("gen_mode", "unknown"),
            raw_sum,
            len(appearing),
            raw_counts,
            sample,
            {k: self._last_wdcg_stats.get(k, 0) for k in ("family_eq1", "family_join_eq1", "family_range1", "family_eqeq", "family_eqrange", "family_rescue")},
            self._last_wdcg_stats.get("width1_count", 0),
            self._last_wdcg_stats.get("width2_count", 0),
            self._last_wdcg_stats.get("seed_count", 0),
            self._last_wdcg_stats.get("eligible_seed_count", 0),
            self._last_wdcg_stats.get("multi_growth_count", 0),
            self._last_wdcg_stats.get("parse_ast_ok", 0),
            self._last_wdcg_stats.get("parse_fallback_regex", 0),
        )
        return query_indexes, appearing

    def _calculate_adaptive_lambda(
        self,
        idx: IndexKey,
        prediction: float,
        observation: float,
        *,
        obs_src: str = "",
        hit_cnt: int = 0,
        ok_cnt: int = 0,
    ) -> float:
        """Adaptive EWMA lambda via Trigg-style tracking signal.

        This restores AdaSelect's core learning mechanism while keeping the new
        candidate generator independent.  Informative observations update RSFE
        and MAD; NO_HIT / all-fallback observations are gated so they do not
        poison the tracking signal.
        """
        if idx not in self.idx_alphas:
            self.idx_error_smooth[idx] = 0.0
            self.idx_abs_error_smooth[idx] = 0.0
            self.idx_alphas[idx] = float(self.alpha_init)
            self.idx_last_err_sign[idx] = 0
            self.idx_sign_smooth[idx] = 0.5
            return float(self.alpha_init)

        if obs_src in ("NO_HIT", "ALL_FALLBACK") or hit_cnt <= 0 or ok_cnt <= 0:
            prev_lam = float(self.idx_alphas.get(idx, float(self.alpha_init)))
            regress = max(0.0, min(1.0, float(getattr(self, "ts_gate_regress", 0.05))))
            lam = (1.0 - regress) * prev_lam + regress * float(self.alpha_init)
            lam = max(float(self.lambda_min), min(float(self.lambda_max), lam))
            self.idx_alphas[idx] = lam
            self._m_stats["ts_gated_updates"] = self._m_stats.get("ts_gated_updates", 0) + 1
            return lam

        q_weight = max(0.0, min(1.0, float(ok_cnt) / float(max(1, hit_cnt))))
        error = (float(observation) - float(prediction)) * q_weight

        old_rsfe = float(self.idx_error_smooth.get(idx, 0.0))
        old_mad = float(self.idx_abs_error_smooth.get(idx, 0.0))

        rho = max(0.0, min(0.9999, float(getattr(self, "rsfe_decay", 0.9))))
        beta = max(0.0, min(1.0, float(getattr(self, "beta_error", 0.20))))
        new_rsfe = rho * old_rsfe + (1.0 - rho) * error
        new_mad = (1.0 - beta) * old_mad + beta * abs(error)

        scale = max(1.0, abs(float(prediction)), abs(float(observation)))
        mad_floor = max(0.0, float(getattr(self, "ts_mad_floor_rel", 1e-6))) * scale
        new_mad = max(new_mad, mad_floor)

        self.idx_error_smooth[idx] = new_rsfe
        self.idx_abs_error_smooth[idx] = new_mad

        prev_sign = int(self.idx_last_err_sign.get(idx, 0))
        sign = 1 if error > 0 else (-1 if error < 0 else 0)
        prev_smooth = float(self.idx_sign_smooth.get(idx, 0.5))
        sign_decay = max(0.0, min(0.9999, float(getattr(self, "ts_sign_decay", 0.90))))
        if sign != 0 and prev_sign != 0:
            same = 1.0 if sign == prev_sign else 0.0
            smooth = sign_decay * prev_smooth + (1.0 - sign_decay) * same
        else:
            smooth = sign_decay * prev_smooth + (1.0 - sign_decay) * 0.5
        self.idx_sign_smooth[idx] = smooth
        if sign != 0:
            self.idx_last_err_sign[idx] = sign

        ts = abs(new_rsfe) / (new_mad + 1e-12)
        base_low = float(getattr(self, "ts_low", 0.50))
        base_high = float(getattr(self, "ts_high", 2.00))
        if smooth >= 0.8:
            mult = 0.7
        elif smooth <= 0.2:
            mult = 1.4
        else:
            mult = 1.0
        ts_low = max(0.05, base_low * mult)
        ts_high = max(ts_low + 0.05, base_high * mult)

        if ts <= ts_low:
            raw_lambda = float(self.lambda_max)
        elif ts >= ts_high:
            raw_lambda = float(self.lambda_min)
        else:
            ratio = (ts - ts_low) / (ts_high - ts_low)
            raw_lambda = float(self.lambda_max) - ratio * (float(self.lambda_max) - float(self.lambda_min))

        lam = max(float(self.lambda_min), min(float(self.lambda_max), raw_lambda))
        self.idx_alphas[idx] = lam
        self._m_stats["ts_updates"] = self._m_stats.get("ts_updates", 0) + 1
        return lam

    def _choose_lambda(
        self,
        idx: IndexKey,
        prev: float,
        obs: float,
        *,
        obs_src: str = "",
        hit_cnt: int = 0,
        ok_cnt: int = 0,
        **_: Any,
    ) -> Tuple[float, float, str]:
        """Return (lambda_used, lambda_shadow, policy).

        In adaptive mode, the adaptive lambda is used. In fixed mode, adaptive
        lambda is still tracked as shadow diagnostics, while the EWMA update uses
        the configured fixed lambda / alpha.
        """
        policy = str(getattr(self, "lambda_policy", "adaptive")).lower()
        if policy in ("fixed", "fix", "const", "constant"):
            lam_shadow = self._calculate_adaptive_lambda(
                idx, prev, obs, obs_src=obs_src, hit_cnt=hit_cnt, ok_cnt=ok_cnt
            )
            lam_used = float(getattr(self, "fixed_lambda", self.alpha_init))
            lam_used = max(0.0, min(1.0, lam_used))
            return lam_used, lam_shadow, policy

        lam_used = self._calculate_adaptive_lambda(
            idx, prev, obs, obs_src=obs_src, hit_cnt=hit_cnt, ok_cnt=ok_cnt
        )
        return lam_used, lam_used, policy

    def _test_candidate(self, idx_key: IndexKey, query_indexes: List[Set[IndexKey]], base_costs: List[float], base_total: float, old_conf: Set[IndexKey], workload: List[str]) -> None:
        tbl, cols = idx_key
        if idx_key in old_conf:
            self.db_con2.disable_index(tbl, cols)
        else:
            self.db_con1.create_index(tbl, cols)
        total_cost = 0.0
        hit_cnt = ok_cnt = fail_cnt = 0
        try:
            for i, (q_idxs, base_cost) in enumerate(zip(query_indexes, base_costs)):
                if idx_key in q_idxs:
                    hit_cnt += 1
                    self._m_stats["what_if_calls"] += 1
                    try:
                        total_cost += float(self.cost_eval.calculate_now_cost([workload[i]]))
                        ok_cnt += 1
                    except Exception as e:
                        logger.warning("what-if failed for q%d idx=%s: %s", i, idx_key, e)
                        total_cost += float(base_cost)
                        fail_cnt += 1
                else:
                    total_cost += float(base_cost)
        finally:
            if idx_key in old_conf:
                self.db_con2.enable_index(tbl, cols)
            else:
                self.db_con1.drop_index(tbl, cols)
        delta = float(base_total - total_cost) if idx_key not in old_conf else float(total_cost - base_total)
        prev = float(self.columns_benefit.get(idx_key, 0.0))
        if hit_cnt <= 0:
            obs_src = "NO_HIT"
        elif ok_cnt <= 0:
            obs_src = "ALL_FALLBACK"
        elif ok_cnt < hit_cnt:
            obs_src = "PARTIAL_FALLBACK"
        else:
            obs_src = "OK"

        lam, lam_shadow, lam_policy = self._choose_lambda(
            idx_key, prev, delta, obs_src=obs_src, hit_cnt=hit_cnt, ok_cnt=ok_cnt, fail_cnt=fail_cnt
        )
        new_benefit = lam * prev + (1.0 - lam) * delta
        self.columns_benefit[idx_key] = new_benefit
        self.idx_alphas[idx_key] = lam
        self.idx_alphas_shadow[idx_key] = lam_shadow
        self.idx_seen_cnt[idx_key] = int(self.idx_seen_cnt.get(idx_key, 0)) + 1
        if len(idx_key[1]) == 1 and delta > 0.0 and obs_src not in ("NO_HIT", "ALL_FALLBACK"):
            self.idx_positive_cnt[idx_key] = int(self.idx_positive_cnt.get(idx_key, 0)) + 1
        self._last_obs_delta_map[idx_key] = delta
        self._last_obs_src_map[idx_key] = obs_src
        self.idx_last_obs_src[idx_key] = obs_src
        logger.debug(
            "benefit %s: %.4f -> %.4f delta=%.4f lambda=%.3f shadow=%.3f policy=%s src=%s hit=%d ok=%d fail=%d",
            idx_key, prev, new_benefit, delta, lam, lam_shadow, lam_policy, obs_src, hit_cnt, ok_cnt, fail_cnt,
        )

    def _record_structural_pair_replacement_diagnostic(
        self,
        pair: IndexKey,
        query_indexes: List[Set[IndexKey]],
        base_costs: List[float],
        base_total: float,
        old_conf: Set[IndexKey],
        workload: List[str],
    ) -> None:
        diag_start = time.perf_counter()
        self._bump_replacement_metric("replacement_probe_count", 1)
        context = self._structural_pair_replacement_context(pair, old_conf)
        left_prefix = context.get("left_prefix_single")
        component_singles = tuple(context.get("component_singles", tuple()) or tuple())
        creation_cost = ""
        try:
            creation_cost = float(self._creation_cost(pair))
        except Exception:
            creation_cost = ""
        diag: Dict[str, Any] = {
            "left_prefix_single": left_prefix,
            "component_singles": component_singles,
            "left_prefix_in_old": bool(left_prefix in old_conf) if left_prefix is not None else False,
            "left_prefix_in_new": False,
            "left_prefix_in_candidate": False,
            "marginal_benefit": float(self._last_obs_delta_map.get(pair, 0.0)),
            "replacement_benefit_raw": "",
            "replacement_benefit": "",
            "replacement_normalized_benefit": "",
            "replacement_creation_cost": creation_cost,
            "replacement_net_benefit": "",
            "replacement_obs_src": "SKIPPED",
        }
        self._last_structural_pair_replacement_map[pair] = diag
        if left_prefix is None or len(pair[1]) != 2:
            self._bump_replacement_metric("replacement_diag_time", (time.perf_counter() - diag_start) * 1000.0)
            return

        tbl, cols = pair
        disabled_left = False
        created_pair = False
        total_cost = 0.0
        ok_cnt = fail_cnt = hit_cnt = 0
        try:
            if left_prefix in old_conf:
                self.db_con2.disable_index(left_prefix[0], left_prefix[1])
                disabled_left = True
            self.db_con1.create_index(tbl, cols)
            created_pair = True
            for i, (q_idxs, base_cost) in enumerate(zip(query_indexes, base_costs)):
                if pair in q_idxs or left_prefix in q_idxs:
                    hit_cnt += 1
                    self._bump_replacement_metric("replacement_what_if_calls", 1)
                    try:
                        total_cost += float(self.cost_eval.calculate_now_cost([workload[i]]))
                        ok_cnt += 1
                    except Exception as exc:
                        logger.warning("replacement what-if failed for q%d pair=%s: %s", i, pair, exc)
                        total_cost += float(base_cost)
                        fail_cnt += 1
                else:
                    total_cost += float(base_cost)
            replacement_benefit_raw = float(base_total - total_cost)
            replacement_normalized_benefit = 0.0
            try:
                scale_map = dict(getattr(self, "columns_benefit", {}) or {})
                scale_map[pair] = max(float(scale_map.get(pair, 0.0) or 0.0), replacement_benefit_raw)
                replacement_normalized_benefit = float(self._log_positive_norm(scale_map).get(pair, 0.0))
            except Exception:
                replacement_normalized_benefit = 0.0
            replacement_creation_cost = float(creation_cost) if creation_cost != "" else 0.0
            diag["replacement_benefit_raw"] = replacement_benefit_raw
            # Backward-compatible alias; raw units are explicit in replacement_benefit_raw.
            diag["replacement_benefit"] = replacement_benefit_raw
            diag["replacement_normalized_benefit"] = replacement_normalized_benefit
            diag["replacement_creation_cost"] = replacement_creation_cost
            diag["replacement_net_benefit"] = replacement_normalized_benefit - replacement_creation_cost
            if hit_cnt <= 0:
                diag["replacement_obs_src"] = "NO_HIT"
            elif ok_cnt <= 0:
                diag["replacement_obs_src"] = "ALL_FALLBACK"
            elif ok_cnt < hit_cnt:
                diag["replacement_obs_src"] = "PARTIAL_FALLBACK"
            else:
                diag["replacement_obs_src"] = "OK"
        except Exception as exc:
            logger.warning("replacement diagnostic failed for pair=%s: %s", pair, exc)
            diag["replacement_obs_src"] = "FAILED"
        finally:
            if created_pair:
                try:
                    self.db_con1.drop_index(tbl, cols)
                except Exception:
                    pass
            if disabled_left:
                try:
                    self.db_con2.enable_index(left_prefix[0], left_prefix[1])
                except Exception:
                    pass
        diag["replacement_hit_count"] = hit_cnt
        diag["replacement_ok_count"] = ok_cnt
        diag["replacement_fail_count"] = fail_cnt
        diag_time = (time.perf_counter() - diag_start) * 1000.0
        diag["replacement_diag_time"] = diag_time
        self._bump_replacement_metric("replacement_hit_count", hit_cnt)
        self._bump_replacement_metric("replacement_ok_count", ok_cnt)
        self._bump_replacement_metric("replacement_fail_count", fail_cnt)
        self._bump_replacement_metric("replacement_diag_time", diag_time)

    @staticmethod
    def _shadow_action_sort_key(action: Dict[str, Any]) -> Tuple[float, str]:
        return (-float(action.get("action_utility", 0.0) or 0.0), str(action.get("action_key", "")))

    @staticmethod
    def _shadow_action_identity(action: Dict[str, Any]) -> str:
        return str(action.get("action_key", ""))

    @staticmethod
    def _dedup_action_sort_key(action: Dict[str, Any]) -> Tuple[float, int, str]:
        type_priority = 0 if str(action.get("action_type", "")) == "REPLACE" else 1
        return (-float(action.get("action_utility", 0.0) or 0.0), type_priority, str(action.get("action_key", "")))

    @classmethod
    def _dedup_actions_for_greedy(cls, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        by_target: Dict[IndexKey, List[Dict[str, Any]]] = {}
        for action in actions:
            key = action.get("index_key", None)
            if isinstance(key, tuple):
                by_target.setdefault(key, []).append(action)
        deduped = [sorted(group, key=cls._dedup_action_sort_key)[0] for group in by_target.values()]
        return sorted(deduped, key=cls._shadow_action_sort_key)

    def _shadow_action_row_for_add(
        self,
        key: IndexKey,
        *,
        norm_map: Dict[IndexKey, float],
        net_map: Dict[IndexKey, float],
        utility_scale_basis: float,
    ) -> Dict[str, Any]:
        creation_cost = float(self._creation_cost(key))
        raw_benefit = float(self.columns_benefit.get(key, 0.0) or 0.0)
        normalized_benefit = self._log_positive_norm_value(raw_benefit, utility_scale_basis)
        utility_source = "raw_benefit_shared_log_scale"
        transition_cost = creation_cost
        utility = float(normalized_benefit) - float(transition_cost)
        return {
            "action_type": "ADD",
            "index_key": key,
            "left_prefix_single": "",
            "pair_key": "",
            "action_key": f"ADD:{self._fmt_index_key(key)}",
            "action_benefit_raw": raw_benefit,
            "action_normalized_benefit": normalized_benefit,
            "action_transition_cost": transition_cost,
            "action_normalized_transition_cost": transition_cost,
            "action_utility": utility,
            "benefit_weight": 1.0,
            "transition_weight": 1.0,
            "utility_scale_basis": float(utility_scale_basis),
            "utility_source": utility_source,
            "alpha_context": float(getattr(self, "alpha_init", 0.0)),
            "beta_context": float(getattr(self, "beta", 0.0)),
        }

    def _shadow_action_row_for_replace(
        self,
        pair: IndexKey,
        diag: Dict[str, Any],
        *,
        utility_scale_basis: float,
    ) -> Optional[Dict[str, Any]]:
        left_prefix = diag.get("left_prefix_single", None)
        if not (isinstance(left_prefix, tuple) and len(left_prefix) == 2):
            return None
        try:
            replacement_net = float(diag.get("replacement_net_benefit", 0.0) or 0.0)
        except Exception:
            replacement_net = 0.0
        if replacement_net <= 0.0:
            return None
        try:
            raw_benefit = float(diag.get("replacement_benefit_raw", diag.get("replacement_benefit", 0.0)) or 0.0)
        except Exception:
            raw_benefit = 0.0
        normalized_benefit = self._log_positive_norm_value(raw_benefit, utility_scale_basis)
        try:
            transition_cost = float(diag.get("replacement_creation_cost", self._creation_cost(pair)) or 0.0)
        except Exception:
            transition_cost = float(self._creation_cost(pair))
        utility = float(normalized_benefit) - float(transition_cost)
        return {
            "action_type": "REPLACE",
            "index_key": pair,
            "left_prefix_single": left_prefix,
            "pair_key": pair,
            "action_key": f"REPLACE:{self._fmt_index_key(left_prefix)}->{self._fmt_index_key(pair)}",
            "action_benefit_raw": raw_benefit,
            "action_normalized_benefit": normalized_benefit,
            "replacement_normalized_benefit_original": diag.get("replacement_normalized_benefit", ""),
            "action_transition_cost": transition_cost,
            "action_normalized_transition_cost": transition_cost,
            "action_utility": utility,
            "benefit_weight": 1.0,
            "transition_weight": 1.0,
            "utility_scale_basis": float(utility_scale_basis),
            "utility_source": "replacement_raw_shared_log_scale",
            "alpha_context": float(getattr(self, "alpha_init", 0.0)),
            "beta_context": float(getattr(self, "beta", 0.0)),
        }

    def _build_shadow_action_table(
        self,
        old_conf: Set[IndexKey],
        candidate_conf: Set[IndexKey],
        *,
        norm_map: Optional[Dict[IndexKey, float]] = None,
        net_map: Optional[Dict[IndexKey, float]] = None,
    ) -> List[Dict[str, Any]]:
        """Build shadow ADD/REPLACE actions from configuration-independent candidate generation evidence.

        This is diagnostic-only: it does not create new candidates, run fresh what-if checks,
        or feed the active selector.
        """
        old_canon = {_canon(k) for k in old_conf}
        norm_map = dict(norm_map or {})
        net_map = dict(net_map or {})
        add_keys = set(candidate_conf or set()) | set(getattr(self, "_last_appearing_set", set()) or set()) | set(getattr(self, "_last_evaluated_set", set()) or set())
        replacement_map = getattr(self, "_last_structural_pair_replacement_map", {}) or {}

        scale_values = [max(0.0, float(v)) for v in getattr(self, "columns_benefit", {}).values()]
        for diag in replacement_map.values():
            if isinstance(diag, dict):
                try:
                    scale_values.append(max(0.0, float(diag.get("replacement_benefit_raw", diag.get("replacement_benefit", 0.0)) or 0.0)))
                except Exception:
                    pass
        utility_scale_basis = max(scale_values) if scale_values else 0.0

        rows: List[Dict[str, Any]] = []
        for key in sorted(add_keys):
            if key in old_canon:
                continue
            rows.append(self._shadow_action_row_for_add(
                key,
                norm_map=norm_map,
                net_map=net_map,
                utility_scale_basis=utility_scale_basis,
            ))

        for pair, diag in sorted(replacement_map.items()):
            if pair in old_canon or not isinstance(diag, dict):
                continue
            row = self._shadow_action_row_for_replace(pair, diag, utility_scale_basis=utility_scale_basis)
            if row is not None:
                rows.append(row)

        rows.sort(key=self._shadow_action_sort_key)
        return rows

    def _apply_shadow_actions_naive(self, old_conf: Set[IndexKey], actions: List[Dict[str, Any]]) -> Tuple[Set[IndexKey], List[Dict[str, Any]], Dict[str, int]]:
        s_shadow: Set[IndexKey] = set(old_conf or set())
        applied: List[Dict[str, Any]] = []
        stats = {
            "naive_replacement_count": 0,
            "naive_add_count": 0,
            "naive_prefix_missing_add_count": 0,
        }
        for action in sorted(actions, key=self._shadow_action_sort_key):
            if float(action.get("action_utility", 0.0) or 0.0) <= 0.0 or len(applied) >= 3:
                break
            idx = action.get("index_key")
            if not isinstance(idx, tuple):
                continue
            next_conf = set(s_shadow)
            applied_action = dict(action)
            stale_converted_to_add = False
            applied_as_replace = False
            if action.get("action_type") == "REPLACE":
                prefix = action.get("left_prefix_single")
                if prefix in next_conf:
                    next_conf.remove(prefix)
                    next_conf.add(idx)
                    applied_as_replace = True
                else:
                    next_conf.add(idx)
                    stale_converted_to_add = True
                    applied_action["stale_replacement_converted_to_add"] = 1
            elif action.get("action_type") == "ADD":
                next_conf.add(idx)
            else:
                continue
            if next_conf == s_shadow:
                continue
            if len(next_conf) > int(self.max_num):
                continue
            s_shadow = next_conf
            applied.append(applied_action)
            if applied_as_replace:
                stats["naive_replacement_count"] += 1
            elif stale_converted_to_add:
                stats["naive_add_count"] += 1
                stats["naive_prefix_missing_add_count"] += 1
            elif action.get("action_type") == "ADD":
                stats["naive_add_count"] += 1
        stats["naive_pair_count"] = sum(1 for key in s_shadow if len(key[1]) == 2)
        return s_shadow, applied, stats

    def _apply_shadow_actions_conflict_aware(self, old_conf: Set[IndexKey], actions: List[Dict[str, Any]]) -> Tuple[Set[IndexKey], List[Dict[str, Any]], Dict[str, int]]:
        s_shadow: Set[IndexKey] = set(old_conf or set())
        applied: List[Dict[str, Any]] = []
        stats = {
            "stale_prefix_missing_count": 0,
            "shadow_transition_add_count": 0,
            "shadow_transition_drop_count": 0,
            "shadow_replacement_count": 0,
        }
        for action in sorted(actions, key=self._shadow_action_sort_key):
            if float(action.get("action_utility", 0.0) or 0.0) <= 0.0 or len(applied) >= 3:
                break
            idx = action.get("index_key")
            if not isinstance(idx, tuple):
                continue
            next_conf = set(s_shadow)
            if action.get("action_type") == "REPLACE":
                prefix = action.get("left_prefix_single")
                if prefix not in next_conf:
                    stats["stale_prefix_missing_count"] += 1
                    continue
                next_conf.remove(prefix)
                next_conf.add(idx)
            elif action.get("action_type") == "ADD":
                next_conf.add(idx)
            else:
                continue
            if next_conf == s_shadow:
                continue
            if len(next_conf) > int(self.max_num):
                continue
            s_shadow = next_conf
            applied.append(dict(action))
            if action.get("action_type") == "REPLACE":
                stats["shadow_transition_add_count"] += 1
                stats["shadow_transition_drop_count"] += 1
                stats["shadow_replacement_count"] += 1
            elif action.get("action_type") == "ADD":
                stats["shadow_transition_add_count"] += 1
        stats["shadow_transition_action_count"] = len(applied)
        stats["shadow_pair_count"] = sum(1 for key in s_shadow if len(key[1]) == 2)
        return s_shadow, applied, stats

    def _record_shadow_action_greedy_diagnostic(
        self,
        old_conf: Set[IndexKey],
        candidate_conf: Set[IndexKey],
        selected_conf: Set[IndexKey],
        *,
        norm_map: Dict[IndexKey, float],
        net_map: Dict[IndexKey, float],
    ) -> None:
        if not isinstance(getattr(self, "_last_wdcg_stats", None), dict):
            self._last_wdcg_stats = {}
        actions = self._build_shadow_action_table(old_conf, candidate_conf, norm_map=norm_map, net_map=net_map)
        greedy_actions = self._dedup_actions_for_greedy(actions)
        naive_conf, naive_actions, naive_stats = self._apply_shadow_actions_naive(old_conf, greedy_actions)
        conflict_conf, conflict_actions, conflict_stats = self._apply_shadow_actions_conflict_aware(old_conf, greedy_actions)

        naive_action_keys = {self._shadow_action_identity(a) for a in naive_actions}
        conflict_action_keys = {self._shadow_action_identity(a) for a in conflict_actions}
        naive_only = sorted(naive_action_keys - conflict_action_keys)
        conflict_only = sorted(conflict_action_keys - naive_action_keys)

        add_actions = [a for a in actions if a.get("action_type") == "ADD"]
        replace_actions = [a for a in actions if a.get("action_type") == "REPLACE"]
        top_add = sorted(add_actions, key=self._shadow_action_sort_key)[:5]
        top_replace = sorted(replace_actions, key=self._shadow_action_sort_key)[:5]

        stats: Dict[str, Any] = {
            "shadow_action_count": len(actions),
            "shadow_add_action_count": len(add_actions),
            "shadow_replace_action_count": len(replace_actions),
            "shadow_greedy_action_count_after_dedup": len(greedy_actions),
            "shadow_duplicate_target_action_count": max(0, len(actions) - len(greedy_actions)),
            "shadow_top_add_actions": self._fmt_actions(top_add),
            "shadow_top_replace_actions": self._fmt_actions(top_replace),
            "shadow_greedy_config_naive": self._fmt_config(naive_conf),
            "shadow_greedy_actions_naive": self._fmt_actions(naive_actions),
            "shadow_greedy_config_conflict_aware": self._fmt_config(conflict_conf),
            "shadow_greedy_actions_conflict_aware": self._fmt_actions(conflict_actions),
            "shadow_greedy_config_stale": self._fmt_config(conflict_conf),
            "shadow_greedy_actions_stale": self._fmt_actions(conflict_actions),
            "shadow_naive_vs_conflict_action_diff_count": len(naive_action_keys.symmetric_difference(conflict_action_keys)),
            "shadow_naive_vs_conflict_config_diff_count": len(set(naive_conf).symmetric_difference(set(conflict_conf))),
            "shadow_naive_only_actions": "|".join(naive_only),
            "shadow_conflict_aware_only_actions": "|".join(conflict_only),
            "shadow_diff_from_active_count": len(set(conflict_conf).symmetric_difference(set(selected_conf or set()))),
            "shadow_diff_from_candidate_count": len(set(conflict_conf).symmetric_difference(set(candidate_conf or set()))),
            "shadow_contains_lineitem_l_partkey_l_shipdate": int(("lineitem", ("l_partkey", "l_shipdate")) in conflict_conf),
            "shadow_contains_orders_o_custkey_o_orderdate": int(("orders", ("o_custkey", "o_orderdate")) in conflict_conf),
        }
        stats.update(naive_stats)
        stats.update(conflict_stats)
        self._last_shadow_action_rows = actions
        self._last_wdcg_stats.update(stats)

    @staticmethod
    def _left_prefix_single(pair: IndexKey) -> Optional[IndexKey]:
        if not (isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[1], tuple) and pair[1]):
            return None
        return (pair[0], (pair[1][0],))

    @classmethod
    def _co_residency_count(cls, conf: Set[IndexKey]) -> int:
        context = set(conf or set())
        count = 0
        for key in context:
            if len(key[1]) < 2:
                continue
            prefix = cls._left_prefix_single(key)
            if prefix in context:
                count += 1
        return count

    def _overlay_opportunity_pairs(self, selected_conf: Set[IndexKey]) -> Set[IndexKey]:
        selected = set(selected_conf or set())
        pairs: Set[IndexKey] = set()
        for pair in set(getattr(self, "_last_structural_pair_candidate_set", set()) or set()):
            if len(pair[1]) != 2 or pair in selected:
                continue
            prefix = self._left_prefix_single(pair)
            if prefix in selected:
                pairs.add(pair)
        return pairs

    def _pair_supply_sets(self) -> Dict[str, Set[IndexKey]]:
        try:
            _gen = getattr(self, "_wdcg_gen", None)
            supply = getattr(_gen, "last_pair_supply", {}) if _gen is not None else {}
            if isinstance(supply, dict):
                return {str(k): set(v or set()) for k, v in supply.items()}
        except Exception:
            pass
        return {}

    @staticmethod
    def _pair_fate_examples(fate_map: Dict[IndexKey, str], fate: str, limit: int = 5) -> str:
        items = sorted(k for k, v in fate_map.items() if v == fate)
        return ";".join(AdaSelect._fmt_index_key(k) for k in items[:limit])

    def _record_pair_supply_diagnostics(
        self,
        *,
        selected_conf: Optional[Set[IndexKey]] = None,
        final_conf: Optional[Set[IndexKey]] = None,
    ) -> None:
        supply = self._pair_supply_sets()
        prequery = set(supply.get("prequery_width2", set()))
        postquery = set(supply.get("postquery_width2", set()))
        dropped_perquery = set(supply.get("dropped_perquery_width2", set()))
        preround = set(supply.get("preround_width2", set()))
        postround = set(supply.get("postround_width2", set()))
        dropped_round = set(supply.get("dropped_round_width2", set()))
        opportunity = set(getattr(self, "_last_overlay_opportunity_pairs", set()) or set())
        admitted = set(getattr(self, "_last_overlay_admitted_pairs", set()) or set())
        fired = set(getattr(self, "_last_overlay_fired_pairs", set()) or set())
        universe = prequery | postquery | dropped_perquery | preround | postround | dropped_round | opportunity | admitted | fired

        fate_map: Dict[IndexKey, str] = {}
        for pair in sorted(universe, key=self._fmt_index_key):
            if pair in fired:
                fate = "lane_admitted_fired"
            elif pair in admitted:
                if not bool(getattr(self, "replacement_overlay_enabled", False)):
                    fate = "lane_admitted_overlay_disabled"
                else:
                    fate = "lane_admitted_blocked_by_eligibility"
            elif pair in opportunity:
                fate = "in_opportunity_blocked_by_lane"
            elif pair in dropped_perquery and pair not in postquery:
                fate = "dropped_perquery_cap"
            elif pair in dropped_round and pair not in postround:
                fate = "dropped_round_cap"
            elif pair in postround or pair in preround or pair in postquery:
                fate = "generated_not_in_overlay_opportunity"
            else:
                fate = "not_generated_other"
            fate_map[pair] = fate

        self._last_pair_fate_map = fate_map
        fate_names = (
            "dropped_perquery_cap",
            "dropped_round_cap",
            "generated_not_in_overlay_opportunity",
            "in_opportunity_blocked_by_lane",
            "lane_admitted_blocked_by_eligibility",
            "lane_admitted_overlay_disabled",
            "lane_admitted_fired",
            "not_generated_other",
        )
        stats: Dict[str, Any] = {"pair_fate_universe_count": int(len(fate_map))}
        for fate in fate_names:
            count = sum(1 for value in fate_map.values() if value == fate)
            stats[f"pair_fate_{fate}_count"] = int(count)
            stats[f"pair_fate_{fate}_examples"] = self._pair_fate_examples(fate_map, fate)
        targets = set(getattr(self, "target_pair_audit", set()) or set())
        selected_set = set(selected_conf if selected_conf is not None else getattr(self, "_last_candidate_conf", set()) or set())
        final_set = set(final_conf if final_conf is not None else getattr(self, "_last_final_conf", set()) or set())
        target_fates = {
            pair: fate_map.get(pair, "not_generated_other")
            for pair in sorted(targets, key=self._fmt_index_key)
        }
        stats.update({
            "target_pair_count": int(len(targets)),
            "target_pair_prequery_coverage_count": int(len(targets & prequery)),
            "target_pair_postquery_coverage_count": int(len(targets & postquery)),
            "target_pair_preround_coverage_count": int(len(targets & preround)),
            "target_pair_postround_coverage_count": int(len(targets & postround)),
            "target_pair_lane_admitted_count": int(len(targets & admitted)),
            "target_pair_selected_count": int(len(targets & selected_set)),
            "target_pair_final_count": int(len(targets & final_set)),
            "target_pair_missing_examples": self._fmt_config(targets - prequery),
            "target_pair_dropped_perquery_examples": self._fmt_config((targets & dropped_perquery) - postquery),
            "target_pair_dropped_round_examples": self._fmt_config((targets & dropped_round) - postround),
            "target_pair_fate_summary": ";".join(
                f"{self._fmt_index_key(pair)}={fate}" for pair, fate in target_fates.items()
            ),
        })
        self._last_wdcg_stats.update(stats)

    @staticmethod
    def _first_overlay_block_reason(reasons: Set[str]) -> str:
        for reason in (
            "no_structural_diag_this_round",
            "pair_not_top_ranked_in_lane",
            "prefix_not_in_selected",
            "pair_already_selected",
            "capacity_exceeded",
            "utility_nonpositive",
            "net_nonpositive",
        ):
            if reason in reasons:
                return reason
        return ""

    def _record_replacement_overlay(self, selected_conf: Set[IndexKey]) -> Set[IndexKey]:
        before_conf = set(selected_conf or set())
        enabled = bool(getattr(self, "replacement_overlay_enabled", False))
        replacement_map = getattr(self, "_last_structural_pair_replacement_map", {}) or {}
        opportunity_pairs = self._overlay_opportunity_pairs(before_conf)
        admitted_pairs = {
            pair for pair in opportunity_pairs
            if pair in replacement_map and isinstance(replacement_map.get(pair), dict)
        }
        self._last_overlay_opportunity_pairs = set(opportunity_pairs)
        self._last_overlay_admitted_pairs = set(admitted_pairs)
        self._last_overlay_fired_pairs = set()
        block_reasons: Set[str] = set()
        if opportunity_pairs and not replacement_map:
            block_reasons.add("no_structural_diag_this_round")
        elif opportunity_pairs and not admitted_pairs:
            block_reasons.add("pair_not_top_ranked_in_lane")

        eligible: List[Tuple[float, float, str, Dict[str, Any]]] = []
        if admitted_pairs:
            replace_rows = [
                dict(action)
                for action in (getattr(self, "_last_shadow_action_rows", []) or [])
                if str(action.get("action_type", "")) == "REPLACE"
            ]
            replace_rows_by_pair: Dict[IndexKey, Dict[str, Any]] = {}
            for action in replace_rows:
                pair = action.get("index_key", None)
                if not isinstance(pair, tuple):
                    continue
                try:
                    utility = float(action.get("action_utility", 0.0) or 0.0)
                except Exception:
                    utility = 0.0
                previous = replace_rows_by_pair.get(pair)
                if previous is None:
                    replace_rows_by_pair[pair] = action
                    continue
                try:
                    previous_utility = float(previous.get("action_utility", 0.0) or 0.0)
                except Exception:
                    previous_utility = 0.0
                if utility > previous_utility or (
                    utility == previous_utility
                    and str(action.get("action_key", "")) < str(previous.get("action_key", ""))
                ):
                    replace_rows_by_pair[pair] = action

            for pair in sorted(admitted_pairs, key=self._fmt_index_key):
                diag = replacement_map.get(pair, {}) if isinstance(replacement_map, dict) else {}
                try:
                    replacement_net = float(diag.get("replacement_net_benefit", 0.0) or 0.0) if isinstance(diag, dict) else 0.0
                except Exception:
                    replacement_net = 0.0
                if replacement_net <= 0.0:
                    block_reasons.add("net_nonpositive")
                    continue

                action = replace_rows_by_pair.get(pair)
                if action is None:
                    block_reasons.add("utility_nonpositive")
                    continue
                prefix = action.get("left_prefix_single", None)
                if not isinstance(prefix, tuple):
                    block_reasons.add("utility_nonpositive")
                    continue
                try:
                    utility = float(action.get("action_utility", 0.0) or 0.0)
                except Exception:
                    utility = 0.0
                next_conf = set(before_conf)
                if prefix not in next_conf:
                    block_reasons.add("prefix_not_in_selected")
                    continue
                if pair in next_conf:
                    block_reasons.add("pair_already_selected")
                    continue
                next_conf.remove(prefix)
                next_conf.add(pair)
                if len(next_conf) > int(self.max_num):
                    block_reasons.add("capacity_exceeded")
                    continue
                if utility <= 0.0:
                    block_reasons.add("utility_nonpositive")
                    continue
                eligible.append((utility, replacement_net, str(action.get("action_key", "")), action))

        after_conf = set(before_conf)
        applied_action: Optional[Dict[str, Any]] = None
        if enabled and eligible:
            _utility, _replacement_net, _action_key, applied_action = sorted(
                eligible,
                key=lambda item: (-item[0], -item[1], item[2]),
            )[0]
            prefix = applied_action.get("left_prefix_single")
            pair = applied_action.get("index_key")
            after_conf = set(before_conf)
            after_conf.remove(prefix)
            after_conf.add(pair)
            if isinstance(pair, tuple):
                self._last_overlay_fired_pairs = {pair}

        applied_count = 1 if applied_action is not None else 0
        diff_count = len(before_conf.symmetric_difference(after_conf))
        selected_action = str(applied_action.get("action_key", "")) if applied_action else ""
        pair_key = applied_action.get("index_key", None) if applied_action else None
        prefix_key = applied_action.get("left_prefix_single", None) if applied_action else None
        utility_value = applied_action.get("action_utility", "") if applied_action else ""
        stats = {
            "replacement_overlay_enabled": int(enabled),
            "replacement_overlay_applied_count": int(applied_count),
            "replacement_overlay_selected_action": selected_action,
            "replacement_overlay_pair": self._fmt_index_key(pair_key) if isinstance(pair_key, tuple) else "",
            "replacement_overlay_prefix": self._fmt_index_key(prefix_key) if isinstance(prefix_key, tuple) else "",
            "replacement_overlay_utility": utility_value,
            "replacement_overlay_before_conf": self._fmt_config(before_conf),
            "replacement_overlay_after_conf": self._fmt_config(after_conf),
            "replacement_overlay_blocked_count": 0 if applied_count else int(bool(block_reasons)),
            "replacement_overlay_block_reason": "" if applied_count else self._first_overlay_block_reason(block_reasons),
            "replacement_overlay_diff_from_topk_count": int(diff_count),
            "overlay_opportunity_rounds": int(bool(opportunity_pairs)),
            "overlay_lane_admitted_rounds": int(bool(admitted_pairs)),
            "overlay_opportunity_pair_count": int(len(opportunity_pairs)),
            "overlay_lane_admitted_pair_count": int(len(admitted_pairs)),
            "overlay_fired_pair_count": int(len(self._last_overlay_fired_pairs)),
            "overlay_blocked_by_lane_count": int(max(0, len(opportunity_pairs - admitted_pairs))),
            "overlay_blocked_by_eligibility_count": int(max(0, len(admitted_pairs) - len(self._last_overlay_fired_pairs))) if enabled else 0,
            "replacement_overlay_co_residency_count": int(self._co_residency_count(after_conf)),
        }
        self._last_wdcg_stats.update(stats)
        return after_conf

    def _estimate_benefits(self, workload: List[str], old_conf: Set[IndexKey]) -> None:
        self._reset_round_diagnostics()
        base_costs, base_total = self._initial_costs(workload)
        self._last_base_total = base_total
        query_indexes, appearing = self._generate_and_merge_candidates(workload, old_conf=old_conf)
        self._last_appearing_set = set(appearing)
        self._m_stats["candidate_count"] += len(appearing)
        self._last_wdcg_stats.update({
            "structural_pair_quota": 0,
            "structural_pair_eval_count": 0,
            "structural_pair_eval_selected_keys": "",
            "structural_pair_eval_budgeted_out_count": 0,
            "structural_pair_eval_lane_enabled": 0,
            "replacement_probe_count": 0,
            "replacement_what_if_calls": 0,
            "replacement_hit_count": 0,
            "replacement_ok_count": 0,
            "replacement_fail_count": 0,
            "replacement_diag_time": 0.0,
        })
        if not appearing:
            logger.info("BenefitBudget | appearing=0 base_total=%.3f", base_total)
            return

        budget = len(appearing) if self.workload_count == 0 else max(1, int(float(self.ratio) * len(appearing)))
        # Robust log-scaled positive benefit keeps a huge winner from flattening medium positives.
        norm_benefit = self._log_positive_norm({idx: self.columns_benefit.get(idx, 0.0) for idx in appearing})
        normal_order = [idx for idx, _ in sorted(norm_benefit.items(), key=lambda kv: (-kv[1], kv[0]))]
        meta_map = self._candidate_meta_map()
        structural_candidates: List[IndexKey] = []
        if str(self._last_wdcg_stats.get("gen_mode", "") or "") == "grow":
            structural_candidates = self._rank_structural_pair_candidates(
                [idx for idx in appearing if self._is_structural_pair_candidate(idx, old_conf, meta_map)],
                meta_map,
            )
        self._last_structural_pair_candidate_set = set(structural_candidates)
        pair_quota = 1 if budget >= 2 and structural_candidates else 0
        selected_structural_pairs = structural_candidates[:pair_quota]
        self._last_structural_pair_lane_set = set(selected_structural_pairs)
        main_budget = max(0, budget - pair_quota)
        main_order = [idx for idx in normal_order if idx not in set(selected_structural_pairs)]
        eval_candidates = selected_structural_pairs + main_order[:main_budget]
        structural_eval_count = sum(1 for idx in structural_candidates if idx in set(eval_candidates))
        structural_budgeted_out = sum(1 for idx in structural_candidates if idx not in set(eval_candidates))
        self._last_eval_order = list(selected_structural_pairs) + list(main_order)
        self._last_wdcg_stats.update({
            "structural_pair_quota": int(pair_quota),
            "structural_pair_eval_count": int(structural_eval_count),
            "structural_pair_eval_selected_keys": ";".join(self._fmt_index_key(k) for k in selected_structural_pairs),
            "structural_pair_eval_budgeted_out_count": int(structural_budgeted_out),
            "structural_pair_eval_lane_enabled": int(pair_quota > 0),
        })
        logger.info(
            "BenefitBudget | base_total=%.3f appearing=%d budget=%d structural_pair_quota=%d eval_order_top=%s",
            base_total, len(appearing), budget, pair_quota, self._last_eval_order[: self.log_candidate_sample],
        )
        trials = 0
        before_whatif = int(self._m_stats["what_if_calls"])
        for idx in eval_candidates:
            self._test_candidate(idx, query_indexes, base_costs, base_total, old_conf, workload)
            self._last_evaluated_set.add(idx)
            trials += 1
            if idx in selected_structural_pairs:
                self._record_structural_pair_replacement_diagnostic(
                    idx, query_indexes, base_costs, base_total, old_conf, workload
                )
        self._m_stats["evaluated_count"] += trials
        logger.info(
            "BenefitEval | evaluated=%d what_if_u=%d what_if_total=%d structural_pair_eval_count=%d evaluated_top=%s",
            trials, int(self._m_stats["what_if_calls"]) - before_whatif, int(self._m_stats["what_if_calls"]), structural_eval_count, list(self._last_evaluated_set)[: self.log_candidate_sample],
        )

        for key in list(self.columns_benefit.keys()):
            if key in appearing:
                continue
            if self.benefit_decay is not None:
                decay = float(self.benefit_decay)
            elif str(getattr(self, "lambda_policy", "adaptive")).lower() in {"fixed", "fix", "const", "constant"}:
                decay = float(getattr(self, "benefit_decay_fixed", 0.95))
            else:
                decay = float(self.idx_alphas.get(key, self.alpha_init))
            decay = max(0.0, min(1.0, decay))
            self.columns_benefit[key] *= decay
            if key in self.idx_error_smooth:
                self.idx_error_smooth[key] *= float(self.rsfe_decay)
            if key in self.idx_abs_error_smooth:
                self.idx_abs_error_smooth[key] *= float(self.rsfe_decay)

    def _choose_config(self, old_conf: Set[IndexKey]) -> Set[IndexKey]:
        old_canon = {_canon(k) for k in old_conf}
        normalized = self._log_positive_norm(self.columns_benefit)
        net: Dict[IndexKey, float] = {}
        for key, val in normalized.items():
            cost = 0.0 if key in old_canon else self._creation_cost(key)
            net[key] = float(val) - float(cost)
        self._last_net_benefit_map = dict(net)
        sorted_keys = sorted(net.items(), key=lambda x: x[1], reverse=True)
        ranked = sorted_keys[: self.max_num]
        filtered_nonpositive_count = sum(
            1 for key, value in ranked
            if key not in old_canon and float(value) <= 0.0
        )
        candidate_conf = {
            key for key, value in ranked
            if key in old_canon or float(value) > 0.0
        }
        self._last_candidate_conf = set(candidate_conf)
        logger.info(
            "Pre-transition pick | candidate=%s filtered_nonpositive_count=%d",
            sorted(candidate_conf),
            filtered_nonpositive_count,
        )

        if self.workload_count == 0:
            selected_conf = set(candidate_conf)
            ratio = float("inf") if candidate_conf else 0.0
            old_benefit = 0.0
            new_benefit = sum(net.get(k, 0.0) for k in selected_conf)
        else:
            old_benefit = sum(net.get(k, 0.0) for k in old_canon)
            new_benefit = sum(net.get(k, 0.0) for k in candidate_conf)
            eps = 1e-9
            ratio = float("-inf")
            if self.transition_mode == "absolute":
                selected_conf = set(candidate_conf) if new_benefit > old_benefit else set(old_canon)
                ratio = new_benefit - old_benefit
            elif self.transition_mode == "relative":
                if abs(old_benefit) > eps:
                    ratio = (new_benefit - old_benefit) / abs(old_benefit)
                selected_conf = set(candidate_conf) if ratio > self.beta else set(old_canon)
            else:
                if old_benefit > eps and new_benefit > eps:
                    ratio = new_benefit / old_benefit
                elif old_benefit < -eps and new_benefit < -eps and abs(new_benefit) > eps:
                    ratio = abs(old_benefit) / abs(new_benefit)
                elif old_benefit < -eps and new_benefit > eps:
                    ratio = float("inf")
                selected_conf = set(candidate_conf) if ratio > self.beta else set(old_canon)
        for diag in getattr(self, "_last_structural_pair_replacement_map", {}).values():
            if not isinstance(diag, dict):
                continue
            left_prefix = diag.get("left_prefix_single", None)
            diag["left_prefix_in_new"] = bool(left_prefix in selected_conf) if left_prefix is not None else False
            diag["left_prefix_in_candidate"] = bool(left_prefix in candidate_conf) if left_prefix is not None else False
        self._record_shadow_action_greedy_diagnostic(
            old_canon,
            set(candidate_conf),
            set(selected_conf),
            norm_map=normalized,
            net_map=net,
        )
        pre_overlay_selected_conf = set(selected_conf)
        selected_conf = self._record_replacement_overlay(set(selected_conf))
        self._record_pair_supply_diagnostics(
            selected_conf=pre_overlay_selected_conf,
            final_conf=set(selected_conf),
        )
        for diag in getattr(self, "_last_structural_pair_replacement_map", {}).values():
            if not isinstance(diag, dict):
                continue
            left_prefix = diag.get("left_prefix_single", None)
            diag["left_prefix_in_new"] = bool(left_prefix in selected_conf) if left_prefix is not None else False
        self._last_final_conf = set(selected_conf)
        self._last_decision_stats = {
            "old_benefit": float(old_benefit),
            "new_benefit": float(new_benefit),
            "ratio": float(ratio),
            "beta": float(self.beta),
            "filtered_nonpositive_count": float(filtered_nonpositive_count),
        }
        self._m_stats["filtered_nonpositive_count"] = self._m_stats.get("filtered_nonpositive_count", 0) + filtered_nonpositive_count
        logger.info(
            "DecisionScore | old=%.4f new=%.4f ratio=%.4f beta=%.4f switched=%d filtered_nonpositive_count=%d",
            old_benefit,
            new_benefit,
            ratio,
            self.beta,
            int(selected_conf != old_canon),
            filtered_nonpositive_count,
        )

        add_set = selected_conf - old_canon
        drop_set = old_canon - selected_conf
        tc_u = sum(self._creation_cost(k) for k in add_set) if add_set else 0.0
        td_u = 0.0
        self._m_stats["reconf_add"] += len(add_set)
        self._m_stats["reconf_drop"] += len(drop_set)
        self._m_stats["trans_create"] += tc_u
        self._m_stats["trans_drop"] += td_u
        logger.info(
            "A-metrics | what_if=%d add_u=%d drop_u=%d trans_create_u=%.3f trans_drop_u=%.3f | add=%d drop=%d trans_create=%.3f trans_drop=%.3f",
            int(self._m_stats["what_if_calls"]), len(add_set), len(drop_set), tc_u, td_u,
            int(self._m_stats["reconf_add"]), int(self._m_stats["reconf_drop"]), float(self._m_stats["trans_create"]), float(self._m_stats["trans_drop"]),
        )
        return set(selected_conf)

    def _handle_timeout_reset(self, old_conf: Set[IndexKey]) -> None:
        logger.warning("Timeout detected - resetting tuner state and dropping all indexes.")
        try:
            self.db_con2.drop_all_indexes()
        except Exception as exc:
            logger.warning("drop_all_indexes during timeout reset failed: %s", exc)
        old_conf.clear()
        self.columns_benefit.clear()
        self.idx_alphas.clear()
        self.idx_alphas_shadow.clear()
        self.idx_error_smooth.clear()
        self.idx_abs_error_smooth.clear()
        self.idx_seen_cnt.clear()
        self.idx_positive_cnt.clear()
        self.idx_first_seen_round.clear()
        self.idx_last_seen_round.clear()
        self.idx_seen_rounds.clear()
        self.idx_last_err_sign.clear()
        self.idx_sign_smooth.clear()
        self.idx_last_obs_src.clear()
        self.workload_count = 0
        self.consecutive_timeouts = 0

    def run(self, workload: List[str], old_conf: Set[IndexKey], runtimes: Optional[List[int]] = None) -> Set[IndexKey]:
        if runtimes and any(float(rt) >= float(self.timeout) for rt in runtimes):
            self._handle_timeout_reset(old_conf)
        self._estimate_benefits(workload, old_conf)
        selected = self._choose_config(old_conf)
        self.workload_count += 1
        return set(selected)


Tuner = AdaSelect
