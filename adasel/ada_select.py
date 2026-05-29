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
        self._last_deadzone_stats = {"deadzone_old_support": 0, "deadzone_blocked": 0}

    @staticmethod
    def _fmt_index_key(key: IndexKey) -> str:
        return f"{key[0]}({','.join(key[1])})"

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
        pair_quota = 1 if budget >= 2 and structural_candidates else 0
        selected_structural_pairs = structural_candidates[:pair_quota]
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
        filtered_nonpositive_count = 0
        if self.workload_count == 0:
            filtered_nonpositive_count = sum(1 for _, value in ranked if float(value) <= 0.0)
            candidate_conf = {key for key, value in ranked if float(value) > 0.0}
        else:
            candidate_conf = {key for key, _ in ranked}
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
        self._last_final_conf = set(selected_conf)
        for diag in getattr(self, "_last_structural_pair_replacement_map", {}).values():
            if not isinstance(diag, dict):
                continue
            left_prefix = diag.get("left_prefix_single", None)
            diag["left_prefix_in_new"] = bool(left_prefix in selected_conf) if left_prefix is not None else False
            diag["left_prefix_in_candidate"] = bool(left_prefix in candidate_conf) if left_prefix is not None else False
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
