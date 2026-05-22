# -*- coding: utf-8 -*-
"""LiteSelectMC – standalone multi‑column online index tuner (parent class).

This version keeps the original selection logic and adds A‑metrics counters so
that LiteSelectA1/A2/A3/A4 can share the same diagnostics:
  - what_if_calls
  - reconf_add / reconf_drop
  - trans_create / trans_drop (seconds-equivalent in parent stays normalized)

It also tolerates both {min,max}_width and {min,max}_index_width keys in JSON.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from util.workload_parser import WorkloadParser
from util.benefit_normalizer import BenefitNormalizer

logger = logging.getLogger(__name__)

# Type aliases
IndexKey = Tuple[str, Tuple[str, ...]]  # (table, (col1, col2, ...))

# Defaults
DEFAULT_COST = 1.0  # creation-cost fallback (normalized)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique_preserve_order(cols: List[str]) -> Tuple[str, ...]:
    seen: Set[str] = set()
    ordered: List[str] = []
    for c in cols:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return tuple(ordered)


def _canon(key: Tuple[Any, ...]) -> IndexKey:
    if len(key) >= 2 and not isinstance(key[1], tuple):
        return (key[0], tuple(key[1:]))
    return key  # already canonical


def _merge_prefixes(idxs: Set[IndexKey]) -> Set[IndexKey]:
    """Keep widest per‑prefix per table to reduce redundancy."""
    by_table: Dict[str, List[IndexKey]] = {}
    for tbl, cols in idxs:
        by_table.setdefault(tbl, []).append((tbl, cols))

    kept: Set[IndexKey] = set()
    for tbl, entries in by_table.items():
        entries.sort(key=lambda x: len(x[1]), reverse=True)
        for ent in entries:
            _, cols = ent
            if not any(cols == big[1][: len(cols)] for big in kept if big[0] == tbl):
                kept.add(ent)
    return kept


# ---------------------------------------------------------------------------
# Parent tuner class
# ---------------------------------------------------------------------------

class LiteSelectMC:
    """Multi‑column index tuner (parent)."""

    IS_MULTI = True

    def __init__(
        self,
        benchmark: str,
        cost_eval,
        db_con1,
        db_con2,
        cfg_path: str = "litesel/config/liteselectmc_topk.json",
        cfg_source: Any = None,
    ) -> None:
        # Core components
        self.cost_eval = cost_eval
        self.db_con1 = db_con1  # what‑if connector
        self.db_con2 = db_con2  # physical connector

        # Configuration (defaults; will be overridden by _load_cfg)
        self.max_num = 10
        self.alpha = 0.65
        self.beta = 1.10
        self.optimizer_ratio = 0.50
        self.timeout = 30000
        self.transition_mode = "symmetric"  # default mode
        self.max_width = 3
        self.min_width = 1

        # Load base & multi‑column configuration.
        # If cfg_source is provided (dict), it overrides cfg_path without touching the JSON file.
        self._load_cfg(cfg_source if cfg_source is not None else cfg_path)

        logger.info(
            "cfg: K=%d α=%.2f β=%.2f ratio=%.2f timeout=%d mode=%s min_w=%d max_w=%d",
            self.max_num,
            self.alpha,
            self.beta,
            self.optimizer_ratio,
            self.timeout,
            self.transition_mode,
            self.min_width,
            self.max_width,
        )

        # State
        self.columns_benefit: Dict[IndexKey, float] = {}
        self.workload_count: int = 0

        # Schema & existing indexes
        self.parser = WorkloadParser(self.db_con1)
        self.tables = self.parser.get_tables()
        self._existing_indexes: Dict[str, Set[IndexKey]] = {}

        # Index cache
        self._cache_indexes()

        # Normalization (creation costs)
        self.benefit_norm = BenefitNormalizer()
        self.benefit_norm.load_creation_costs(benchmark)

        # A‑metrics counters (added to keep parity with A3/A4)
        self._m_stats = {
            "what_if_calls": 0,
            "candidate_count": 0,
            "evaluated_count": 0,
            "reconf_add": 0,
            "reconf_drop": 0,
            "trans_create": 0.0,
            "trans_drop": 0.0,
        }

        # Expose last round's observed base workload cost (under old_conf).
        # The Phase 0.2 drivers log this as per-round exec cost.
        self._last_base_total = 0.0

        # Phase 0.3/0.4 trace support (main.py decides whether to persist it).
        # - _last_evaluated_set: which candidates were actually evaluated (what-if).
        # - _last_eval_order: ranking/order used when allocating budget.
        self._last_evaluated_set: Set[IndexKey] = set()
        self._last_eval_order: List[IndexKey] = []

    # --------------------------- config & cache ---------------------------
    def _load_cfg(self, source: Any) -> None:
        """Load configuration from JSON file path or an in-memory dict.

        Phase 0.2: we prefer dict injection so that CLI overrides do NOT require
        mutating the JSON file on disk.
        """
        try:
            if isinstance(source, dict):
                cfg = source
            else:
                with open(str(source), encoding="utf-8") as f:
                    cfg = json.load(f)

            self.max_num = cfg.get("max_num", self.max_num)
            self.alpha = cfg.get("alpha", self.alpha)
            self.beta = cfg.get("beta", self.beta)
            self.optimizer_ratio = cfg.get("optimizer_ratio", cfg.get("ratio", self.optimizer_ratio))
            self.timeout = cfg.get("timeout", self.timeout)
            self.transition_mode = cfg.get("transition_mode", self.transition_mode)
            # accept either {min,max}_width or {min,max}_index_width
            self.max_width = cfg.get("max_width", cfg.get("max_index_width", self.max_width))
            self.min_width = cfg.get("min_width", cfg.get("min_index_width", self.min_width))
        except FileNotFoundError:
            logger.warning("Config file not found: %s. Using defaults.", str(source))
        except Exception as e:
            logger.error("Failed to load config %s: %s", str(source), e)

    def _cache_indexes(self) -> None:
        """Cache existing PRIMARY KEY / UNIQUE indexes to avoid re‑creating them."""
        for tbl in self.tables:
            defs = self.db_con1.exec_fetchall(
                f"SELECT indexdef FROM pg_indexes WHERE tablename = '{tbl}'"
            )
            idxs: Set[IndexKey] = set()
            for (idxdef,) in defs:
                if " UNIQUE " in idxdef or "PRIMARY KEY" in idxdef:
                    m = re.search(r"\(([^)]+)\)", idxdef)
                    if m:
                        cols = tuple(col.strip().strip('"') for col in m.group(1).split(','))
                        idxs.add((tbl, cols))
            self._existing_indexes[tbl] = idxs
        logger.debug("Cached PK/UNIQUE: %s", self._existing_indexes)

    # --------------------------- utilities ---------------------------
    @staticmethod
    def _minmax_norm_k(vals: Dict[Any, float], k: Optional[int] = None) -> Dict[Any, float]:
        if not vals:
            return {}
        values = list(vals.values())
        if k is not None:
            tv = sorted(values, reverse=True)[: max(1, k)]
            hi, lo = max(tv), min(tv)
        else:
            hi, lo = max(values), min(values)
        rng = hi - lo
        if rng <= 1e-9:
            return {key: 0.0 for key in vals}
        return {key: (v - lo) / rng for key, v in vals.items()}

    @staticmethod
    def _minmax_norm(data: Dict[IndexKey, float]) -> Dict[IndexKey, float]:
        """
        Robust Max-Scale Normalization with Quantile Clipping.
        """
        if not data:
            return {}

        vals = list(data.values())
        positive_vals = [v for v in vals if v > 1e-9]

        scale_factor = 1.0
        if positive_vals:
            sorted_pos = sorted(positive_vals)
            p95_index = int(len(sorted_pos) * 0.95)
            p95_index = min(p95_index, len(sorted_pos) - 1)
            p95_val = sorted_pos[p95_index]
            if p95_val > 1e-9:
                scale_factor = p95_val

        normalized = {}
        for k, v in data.items():
            norm_v = v / scale_factor
            if norm_v > 1.0:
                norm_v = 1.0
            if norm_v < -2.0:
                norm_v = -2.0
            normalized[k] = norm_v
        return normalized

    def _creation_cost(self, key: IndexKey) -> float:
        cols = key[1]
        return self.benefit_norm.index_costs.get(cols, DEFAULT_COST)

    # --------------------------- core flow ---------------------------
    def _initial_costs(self, workload: List[str]) -> Tuple[List[float], float]:
        costs = [self.cost_eval.calculate_now_cost([q]) for q in workload]
        return costs, sum(costs)

    def _generate_and_merge_candidates(
        self, workload: List[str]
    ) -> Tuple[List[Set[IndexKey]], Set[IndexKey]]:
        query_indexes: List[Set[IndexKey]] = []
        appearing: Set[IndexKey] = set()
        for q in workload:
            idx_map = self.parser.store_indexable_columns(q, self.tables)
            q_set: Set[IndexKey] = set()
            for tbl, cols in idx_map.items():
                if not cols:
                    continue
                uniq_cols = _unique_preserve_order(cols)
                up_to = min(self.max_width, len(uniq_cols))
                for w in range(self.min_width, up_to + 1):
                    for combo in itertools.permutations(uniq_cols, w):
                        key: IndexKey = (tbl, combo)
                        if key in self._existing_indexes.get(tbl, set()):
                            continue
                        q_set.add(key)
                        self.columns_benefit.setdefault(key, 0.0)
            merged = _merge_prefixes(q_set)
            query_indexes.append(merged)
            appearing.update(merged)
        appearing = _merge_prefixes(appearing)
        return query_indexes, appearing

    def _estimate_benefits(
        self,
        workload: List[str],
        base_costs: List[float],
        base_total: float,
        old_conf: Set[IndexKey],
    ) -> None:
        # Phase 0.3/0.4 trace helpers: reset per-round state.
        self._last_evaluated_set.clear()
        self._last_eval_order.clear()

        query_indexes, appearing = self._generate_and_merge_candidates(workload)
        self._m_stats["candidate_count"] += len(appearing)
        if not appearing:
            return

        budget = len(appearing) if self.workload_count == 0 else max(
            1, int(self.optimizer_ratio * len(appearing))
        )
        trials = 0

        # Record the priority order considered for evaluation (restricted to those
        # that appear in this round). This is useful to debug oscillations caused
        # by budget cut-off / rank boundary effects.
        self._last_eval_order = [
            idx_key
            for idx_key, _ in sorted(
                self.columns_benefit.items(), key=lambda kv: kv[1], reverse=True
            )
            if idx_key in appearing
        ]

        for idx_key, _ in sorted(
            self.columns_benefit.items(), key=lambda kv: kv[1], reverse=True
        ):
            if trials >= budget:
                break
            if idx_key not in appearing:
                continue
            self._test_candidate(
                idx_key, query_indexes, base_costs, base_total, old_conf, workload
            )
            self._last_evaluated_set.add(idx_key)
            trials += 1

        self._m_stats["evaluated_count"] += trials

        for key in self.columns_benefit:
            if key not in appearing:
                self.columns_benefit[key] *= self.alpha

    def _test_candidate(
        self,
        idx_key: IndexKey,
        query_indexes: List[Set[IndexKey]],
        base_costs: List[float],
        base_total: float,
        old_conf: Set[IndexKey],
        workload: List[str],
    ) -> None:
        tbl, cols = idx_key
        if idx_key in old_conf:
            self.db_con2.disable_index(tbl, cols)
        else:
            self.db_con1.create_index(tbl, cols)

        total_cost = 0.0
        for i, (q_idxs, base_cost) in enumerate(zip(query_indexes, base_costs)):
            if idx_key in q_idxs:
                self._m_stats["what_if_calls"] += 1
                try:
                    c = self.cost_eval.calculate_now_cost([workload[i]])
                    total_cost += c
                except Exception as e:
                    logger.warning("what‑if failed for q%d idx=%s: %s", i, idx_key, e)
                    total_cost += base_cost
            else:
                total_cost += base_cost

        if idx_key in old_conf:
            self.db_con2.enable_index(tbl, cols)
        else:
            self.db_con1.drop_index(tbl, cols)

        delta = base_total - total_cost if idx_key not in old_conf else total_cost - base_total
        prev = self.columns_benefit.get(idx_key, 0.0)
        self.columns_benefit[idx_key] = self.alpha * prev + (1 - self.alpha) * delta
        logger.debug("benefit %s: %.4f → %.4f (Δ=%.4f)", idx_key, prev, self.columns_benefit[idx_key], delta)

    def _choose_config(self, old_conf: Set[IndexKey]) -> List[IndexKey]:
        """Parent selection rule; unchanged semantics.
        Only A‑metrics bookkeeping is added.
        """
        old_canon = {_canon(k) for k in old_conf}

        normalized = self._minmax_norm(self.columns_benefit)

        net_benefits: Dict[IndexKey, float] = {}
        for key, val in normalized.items():
            cost = 0.0
            if key not in old_canon:
                cost = self._creation_cost(key)
            net_benefits[key] = val - cost

        sorted_keys = sorted(net_benefits.items(), key=lambda x: x[1], reverse=True)
        candidate_conf = {key for key, _ in sorted_keys[: self.max_num]}
        logger.debug("Pre‑transition pick: %s", sorted(candidate_conf))

        if self.workload_count == 0:
            add_set = candidate_conf - old_canon
            drop_set = old_canon - candidate_conf
            add_u = len(add_set)
            drop_u = len(drop_set)
            tc_u = sum(self._creation_cost(k) for k in add_set) if add_set else 0.0
            td_u = 0.0

            self._m_stats["reconf_add"] += add_u
            self._m_stats["reconf_drop"] += drop_u
            self._m_stats["trans_create"] += tc_u
            self._m_stats["trans_drop"] += td_u

            logger.info(
                "A-metrics | what_if=%d add_u=%d drop_u=%d trans_create_u=%.3f trans_drop_u=%.3f | "
                "add=%d drop=%d trans_create=%.3f trans_drop=%.3f",
                self._m_stats["what_if_calls"],
                add_u,
                drop_u,
                tc_u,
                td_u,
                self._m_stats["reconf_add"],
                self._m_stats["reconf_drop"],
                self._m_stats["trans_create"],
                self._m_stats["trans_drop"],
            )
            return sorted(candidate_conf)

        # 5) transition logic (Phase 0.2 fix):
        # Replace "old≈0 -> ratio=inf (force switch)" with AdaSelect-style dead-zone.
        old_benefit = sum(net_benefits.get(k, 0.0) for k in old_canon)
        new_benefit = sum(net_benefits.get(k, 0.0) for k in candidate_conf)
        logger.debug("Net benefit: old=%.4f new=%.4f", old_benefit, new_benefit)

        selected_conf = old_canon
        mode = self.transition_mode
        eps = 1e-9

        if mode == "absolute":
            if new_benefit > old_benefit:
                selected_conf = candidate_conf

        elif mode == "relative":
            # Keep the original "relative improvement" definition, but apply dead-zone:
            # if |old| is too small, do NOT switch (ratio stays -inf).
            ratio = float("-inf")
            old_abs = abs(old_benefit)
            if old_abs > eps:
                ratio = (new_benefit - old_benefit) / old_abs
            if ratio > self.beta:
                selected_conf = candidate_conf

        else:  # symmetric (AdaSelect-style dead-zone)
            ratio = float("-inf")
            if old_benefit > eps and new_benefit > eps:
                ratio = new_benefit / old_benefit
            elif old_benefit < -eps and new_benefit < -eps:
                ratio = abs(old_benefit) / abs(new_benefit) if abs(new_benefit) > eps else float("-inf")
            elif old_benefit < -eps and new_benefit > eps:
                ratio = float("inf")  # negative -> positive is always better

            if ratio > self.beta:
                selected_conf = candidate_conf

        final_set = selected_conf
        add_set = final_set - old_canon
        drop_set = old_canon - final_set
        add_u = len(add_set)
        drop_u = len(drop_set)
        tc_u = sum(self._creation_cost(k) for k in add_set) if add_set else 0.0
        td_u = 0.0

        self._m_stats["reconf_add"] += add_u
        self._m_stats["reconf_drop"] += drop_u
        self._m_stats["trans_create"] += tc_u
        self._m_stats["trans_drop"] += td_u

        logger.info(
            "A-metrics | what_if=%d add_u=%d drop_u=%d trans_create_u=%.3f trans_drop_u=%.3f | "
            "add=%d drop=%d trans_create=%.3f trans_drop=%.3f",
            self._m_stats["what_if_calls"],
            add_u,
            drop_u,
            tc_u,
            td_u,
            self._m_stats["reconf_add"],
            self._m_stats["reconf_drop"],
            self._m_stats["trans_create"],
            self._m_stats["trans_drop"],
        )
        return sorted(final_set)

    # --------------------------- entry point ---------------------------
    def run(
        self,
        workload: List[str],
        old_conf: Set[IndexKey],
        runtimes: List[int],
    ) -> Set[IndexKey]:
        if any(rt >= self.timeout for rt in runtimes):
            logger.warning("Timeout detected – resetting tuner state and dropping all indexes.")
            self.db_con2.drop_all_indexes()
            old_conf.clear()
            self.columns_benefit.clear()
            self.workload_count = 0

        base_costs, base_total = self._initial_costs(workload)
        self._last_base_total = float(base_total)
        self._estimate_benefits(workload, base_costs, base_total, old_conf)
        selected = self._choose_config(old_conf)
        self.workload_count += 1
        return set(selected)


# For dynamic loader
Tuner = LiteSelectMC
