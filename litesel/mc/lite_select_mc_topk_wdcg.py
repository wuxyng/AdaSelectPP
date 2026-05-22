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
import hashlib
import math
from collections import Counter, defaultdict
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
# Phase 0.5 option: WDCG-based candidate pruning / ordering (plan-first)
# ---------------------------------------------------------------------------

# Plan regexes (borrowed from the hardened plan-first TemplateExtractor).
_CMT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_CMT_LINE = re.compile(r"--[^\n]*")
_COLREF_RE = re.compile(r'"?([A-Za-z_][\w$]*)"?\s*\.\s*"?(?P<col>[A-Za-z_][\w$]*)"?')
_JOIN_EQ_RE = re.compile(
    r'"?([A-Za-z_][\w$]*)"?\s*\.\s*"?(?P<c1>[A-Za-z_][\w$]*)"?(?:\s*::\s*[\w\.]+)?\s*=\s*'
    r'"?([A-Za-z_][\w$]*)"?\s*\.\s*"?(?P<c2>[A-Za-z_][\w$]*)"?(?:\s*::\s*[\w\.]+)?',
    re.I,
)
_EQ_OP_RE = re.compile(r"(?<![<>=!])=(?![<>=])|\b(in)\b|\b(is)\b", re.I)
_RNG_OP_RE = re.compile(r"<=|>=|<|>|\b(between)\b|\b(like)\b|\bilike\b|\bsimilar\b", re.I)


def _strip_sql_comments(sql: str) -> str:
    sql = sql or ""
    sql = _CMT_BLOCK.sub(" ", sql)
    sql = _CMT_LINE.sub(" ", sql)
    return sql


def _strip_literals_for_sig(sql: str) -> str:
    """Best-effort SQL canonicalization for cache keys (no heavy parser)."""
    s = _strip_sql_comments(sql or "")
    # strings
    s = re.sub(r"'(?:''|[^'])*'", "?", s)
    # numbers (ints/floats)
    s = re.sub(r"\b\d+(?:\.\d+)?\b", "?", s)
    # whitespace
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _sql_sig(sql: str) -> str:
    s = _strip_literals_for_sig(sql)
    return hashlib.md5(s.encode("utf-8", errors="ignore")).hexdigest()


def _norm_ident(x: str) -> str:
    x = (x or "").strip()
    if x.startswith('"') and x.endswith('"') and len(x) >= 2:
        x = x[1:-1]
    if "." in x:
        x = x.split(".")[-1]
    return x.strip()


class _PlanRoleExtractor:
    """Extract (table -> role -> Counter[col]) from Postgres JSON plan."""

    def __init__(self, db_con) -> None:
        self.db_con = db_con
        self._plan_cache: Dict[str, Dict[str, Any]] = {}  # sig -> root node
        self._rows_cache: Dict[str, float] = {}

    # ---- plan IO ----
    def get_root_plan(self, sql: str) -> Optional[Dict[str, Any]]:
        sig = _sql_sig(sql)
        if sig in self._plan_cache:
            return self._plan_cache[sig]

        plan_obj: Any = None
        if hasattr(self.db_con, "get_plan"):
            try:
                plan_obj = self.db_con.get_plan(sql)  # type: ignore[attr-defined]
            except Exception:
                plan_obj = None

        if plan_obj is None:
            # fallback: run EXPLAIN ourselves (FORMAT JSON returns a single row with a JSON array)
            try:
                rows = self.db_con.exec_fetchall(f"EXPLAIN (FORMAT JSON, VERBOSE) {sql}")  # type: ignore[attr-defined]
                if rows and rows[0]:
                    plan_obj = rows[0][0]
            except Exception:
                plan_obj = None

        root = self._extract_root_node(plan_obj)
        if isinstance(root, dict):
            self._plan_cache[sig] = root
            return root
        return None

    @staticmethod
    def _extract_root_node(plan_obj: Any) -> Optional[Dict[str, Any]]:
        """Accept several common shapes: dict with Plan, dict node, list[dict], list[list]."""
        if plan_obj is None:
            return None
        # if returned as JSON string
        if isinstance(plan_obj, str):
            try:
                plan_obj = json.loads(plan_obj)
            except Exception:
                return None

        # common: list with single dict
        if isinstance(plan_obj, list) and plan_obj:
            # EXPLAIN (FORMAT JSON) typically returns a list with one dict
            first = plan_obj[0]
            if isinstance(first, dict):
                if "Plan" in first and isinstance(first["Plan"], dict):
                    return first["Plan"]
                if "Node Type" in first:
                    return first
            # sometimes nested list
            if isinstance(first, list) and first and isinstance(first[0], dict):
                inner = first[0]
                if "Plan" in inner and isinstance(inner["Plan"], dict):
                    return inner["Plan"]
                if "Node Type" in inner:
                    return inner

        if isinstance(plan_obj, dict):
            if "Plan" in plan_obj and isinstance(plan_obj["Plan"], dict):
                return plan_obj["Plan"]
            if "Node Type" in plan_obj:
                return plan_obj
        return None

    # ---- table size (optional prune) ----
    def reltuples(self, table: str) -> float:
        t = _norm_ident(table)
        if t in self._rows_cache:
            return self._rows_cache[t]
        rows = 0.0
        try:
            sql = (
                "SELECT c.reltuples::float8 "
                "  FROM pg_class c "
                "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                f" WHERE n.nspname='public' AND c.relname='{t}'"
            )
            res = self.db_con.exec_fetchall(sql)  # type: ignore[attr-defined]
            if res and res[0] and res[0][0] is not None:
                rows = float(res[0][0])
        except Exception:
            rows = 0.0
        self._rows_cache[t] = rows
        return rows

    # ---- extract roles ----
    def extract_role_counters(self, sql: str, min_table_ratio: float = 0.0) -> Dict[str, Dict[str, Counter]]:
        root = self.get_root_plan(sql)
        if not isinstance(root, dict):
            return {}

        alias2table: Dict[str, str] = {}
        tables: Set[str] = set()
        self._collect_aliases(root, alias2table, tables)

        # optional per-query small-table pruning
        keep_tables = set(tables)
        if min_table_ratio and len(tables) > 1:
            try:
                rows_map = {t: self.reltuples(t) for t in tables}
                mx = max(rows_map.values()) if rows_map else 0.0
                if mx > 0:
                    thr = mx * float(min_table_ratio)
                    keep_tables = {t for t, r in rows_map.items() if r >= thr}
                    if not keep_tables:
                        keep_tables = set(tables)
                    if len(keep_tables) == 1 and len(tables) > 1:
                        keep_tables = set(sorted(rows_map.keys(), key=lambda x: rows_map[x], reverse=True)[:2])
            except Exception:
                keep_tables = set(tables)

        roles: Dict[str, Dict[str, Counter]] = {}
        for t in keep_tables:
            roles[t] = {
                "join_eq": Counter(),
                "filter_eq": Counter(),
                "filter_rng": Counter(),
                "group_by": Counter(),
                "order_by": Counter(),
            }

        self._walk(root, alias2table, roles)
        return roles

    def _collect_aliases(self, node: Dict[str, Any], alias2table: Dict[str, str], tables: Set[str]) -> None:
        rel = node.get("Relation Name")
        if rel:
            t = _norm_ident(str(rel))
            tables.add(t)
            alias = _norm_ident(str(node.get("Alias") or t))
            if alias:
                alias2table[alias] = t
            alias2table[t] = t

        for ch in node.get("Plans", []) or []:
            if isinstance(ch, dict):
                self._collect_aliases(ch, alias2table, tables)

    def _walk(self, node: Dict[str, Any], alias2table: Dict[str, str], roles: Dict[str, Dict[str, Counter]]) -> None:
        # join conditions
        for key in ("Hash Cond", "Merge Cond", "Join Filter"):
            expr = node.get(key)
            if expr:
                self._ingest_expr(str(expr), alias2table, roles, source="join")

        # filters (Index Cond may include join keys under NLJ)
        for key in ("Filter", "Index Cond", "Recheck Cond"):
            expr = node.get(key)
            if expr:
                self._ingest_expr(str(expr), alias2table, roles, source="filter" if key != "Index Cond" else "index_cond")

        # group/sort keys
        gk = node.get("Group Key")
        if isinstance(gk, list):
            for e in gk:
                self._ingest_key(str(e), alias2table, roles, role="group_by")
        sk = node.get("Sort Key")
        if isinstance(sk, list):
            for e in sk:
                self._ingest_key(str(e), alias2table, roles, role="order_by")

        for ch in node.get("Plans", []) or []:
            if isinstance(ch, dict):
                self._walk(ch, alias2table, roles)

    def _map_alias(self, alias: str, alias2table: Dict[str, str], roles: Dict[str, Dict[str, Counter]]) -> Optional[str]:
        a = _norm_ident(alias)
        if not a:
            return None
        t = alias2table.get(a)
        if t and t in roles:
            return t
        if a in roles:
            return a
        return None

    def _find_colrefs(self, expr: str) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for m in _COLREF_RE.finditer(expr or ""):
            a = _norm_ident(m.group(1))
            c = _norm_ident(m.group("col")).lower()
            if a and c:
                out.append((a, c))
        return out

    def _ingest_key(self, expr: str, alias2table: Dict[str, str], roles: Dict[str, Dict[str, Counter]], role: str) -> None:
        for a, c in self._find_colrefs(expr):
            t = self._map_alias(a, alias2table, roles)
            if t:
                roles[t][role][c] += 1

    def _ingest_expr(self, expr: str, alias2table: Dict[str, str], roles: Dict[str, Dict[str, Counter]], source: str) -> None:
        # 1) join pairs (col = col)
        join_cols: Set[Tuple[str, str]] = set()
        for m in _JOIN_EQ_RE.finditer(expr or ""):
            a1 = _norm_ident(m.group(1))
            c1 = _norm_ident(m.group("c1")).lower()
            a2 = _norm_ident(m.group(3))
            c2 = _norm_ident(m.group("c2")).lower()
            t1 = self._map_alias(a1, alias2table, roles)
            t2 = self._map_alias(a2, alias2table, roles)
            if t1:
                roles[t1]["join_eq"][c1] += 1
                join_cols.add((a1, c1))
            if t2:
                roles[t2]["join_eq"][c2] += 1
                join_cols.add((a2, c2))

        # 2) classify as filter eq/rng
        is_rng = bool(_RNG_OP_RE.search(expr or ""))
        is_eq = bool(_EQ_OP_RE.search(expr or ""))

        if not (is_rng or is_eq):
            return

        role_name = "filter_rng" if is_rng else "filter_eq"

        for a, c in self._find_colrefs(expr):
            # NLJ guard: when source is Index Cond, col=col should be treated as join, not filter.
            if source == "index_cond" and (a, c) in join_cols:
                continue
            t = self._map_alias(a, alias2table, roles)
            if t:
                roles[t][role_name][c] += 1


def _dcg_pos_weight(pos0: int) -> float:
    """pos0 is 0-based; return 1/log2(2+pos)."""
    return 1.0 / math.log2(2.0 + float(pos0))


def _wdcg_score_index(cols: Tuple[str, ...], col_w: Dict[str, float]) -> float:
    s = 0.0
    for j, c in enumerate(cols):
        w = col_w.get(c.lower(), 0.0)
        if w <= 0:
            continue
        s += w * _dcg_pos_weight(j)
    return s

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
        cfg_path: str = "litesel/config/LiteSelectMC_topk.json",
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

        # Phase 0.5 optional: WDCG-based candidate pruning / ordering.
        # When enabled, we rank and prune candidates using plan-derived role columns
        # (join/filter/group/order). This helps under recurrent/shifting workloads.
        self.wdcg_enabled = False
        self.wdcg_use_plan = True  # kept for symmetry with AdaSelect++; plan-first is recommended.
        self.wdcg_topk = 1000
        self.wdcg_family_cap = 2
        self.wdcg_small_table_prune = True
        self.wdcg_min_table_ratio = 0.05  # relative to max table size within a query

        # role weights (can be overridden via cfg)
        self.wdcg_w_join = 3.0
        self.wdcg_w_filter_eq = 2.0
        self.wdcg_w_filter_rng = 1.5
        self.wdcg_w_group = 1.0
        self.wdcg_w_order = 1.0

        # internal extractor (initialized after cfg is loaded)
        self._wdcg_extractor = None


        # Load base & multi‑column configuration.
        # If cfg_source is provided (dict), it overrides cfg_path without touching the JSON file.
        self._load_cfg(cfg_source if cfg_source is not None else cfg_path)

        # Initialize plan-first role extractor if WDCG is enabled.
        if self.wdcg_enabled and self.db_con1 is not None:
            try:
                self._wdcg_extractor = _PlanRoleExtractor(self.db_con1)
            except Exception:
                self._wdcg_extractor = None
        else:
            self._wdcg_extractor = None


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
            # Phase 0.5 optional: WDCG knobs
            self.wdcg_enabled = cfg.get("wdcg", cfg.get("wdcg_enabled", self.wdcg_enabled))
            self.wdcg_use_plan = cfg.get("wdcg_use_plan", self.wdcg_use_plan)
            self.wdcg_topk = int(cfg.get("wdcg_topk", self.wdcg_topk))
            self.wdcg_family_cap = int(cfg.get("wdcg_family_cap", self.wdcg_family_cap))
            self.wdcg_small_table_prune = bool(cfg.get("wdcg_small_table_prune", self.wdcg_small_table_prune))
            self.wdcg_min_table_ratio = float(cfg.get("wdcg_min_table_ratio", self.wdcg_min_table_ratio))
            self.wdcg_w_join = float(cfg.get("wdcg_w_join", self.wdcg_w_join))
            self.wdcg_w_filter_eq = float(cfg.get("wdcg_w_filter_eq", self.wdcg_w_filter_eq))
            self.wdcg_w_filter_rng = float(cfg.get("wdcg_w_filter_rng", self.wdcg_w_filter_rng))
            self.wdcg_w_group = float(cfg.get("wdcg_w_group", self.wdcg_w_group))
            self.wdcg_w_order = float(cfg.get("wdcg_w_order", self.wdcg_w_order))
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

    # --------------------------- Phase 0.5: WDCG pruning ---------------------------
    def _wdcg_prepare_eval(
        self,
        workload: List[str],
        appearing: Set[IndexKey],
    ) -> Tuple[Set[IndexKey], List[IndexKey]]:
        """Return (eval_set, eval_order) based on WDCG scores.

        eval_order is a list of candidates sorted by WDCG score (ties broken by
        current benefit estimate). eval_set is top-k after family-cap pruning.
        """
        if not self.wdcg_enabled or not appearing:
            return set(appearing), []

        if self._wdcg_extractor is None:
            # If extractor is unavailable, do NOT risk unstable SQL parsing.
            return set(appearing), []

        # 1) Aggregate per-table column weights from plan-derived roles.
        col_w_by_table: Dict[str, Dict[str, float]] = defaultdict(dict)
        min_ratio = self.wdcg_min_table_ratio if self.wdcg_small_table_prune else 0.0

        for sql in workload:
            try:
                roles = self._wdcg_extractor.extract_role_counters(sql, min_table_ratio=min_ratio)
            except Exception:
                roles = {}
            for tbl, rc in roles.items():
                tbl = str(tbl)
                col_w = col_w_by_table.setdefault(tbl, {})
                for c, cnt in rc.get("join_eq", {}).items():
                    col_w[c] = col_w.get(c, 0.0) + float(cnt) * float(self.wdcg_w_join)
                for c, cnt in rc.get("filter_eq", {}).items():
                    col_w[c] = col_w.get(c, 0.0) + float(cnt) * float(self.wdcg_w_filter_eq)
                for c, cnt in rc.get("filter_rng", {}).items():
                    col_w[c] = col_w.get(c, 0.0) + float(cnt) * float(self.wdcg_w_filter_rng)
                for c, cnt in rc.get("group_by", {}).items():
                    col_w[c] = col_w.get(c, 0.0) + float(cnt) * float(self.wdcg_w_group)
                for c, cnt in rc.get("order_by", {}).items():
                    col_w[c] = col_w.get(c, 0.0) + float(cnt) * float(self.wdcg_w_order)

        # 2) Score each candidate.
        scored: List[Tuple[IndexKey, float, float]] = []
        for idx in appearing:
            tbl, cols = idx
            col_w = col_w_by_table.get(tbl, {})
            s = _wdcg_score_index(cols, col_w)
            b = float(self.columns_benefit.get(idx, 0.0))
            scored.append((idx, float(s), b))

        scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
        eval_order = [x[0] for x in scored]

        # 3) Apply family cap + top-k.
        cap = int(self.wdcg_family_cap) if self.wdcg_family_cap is not None else 0
        k = max(1, int(self.wdcg_topk)) if self.wdcg_topk is not None else len(eval_order)

        fam_cnt: Dict[Tuple[str, str], int] = defaultdict(int)
        picked: List[IndexKey] = []

        for idx in eval_order:
            if len(picked) >= k:
                break
            tbl, cols = idx
            first = cols[0] if cols else ""
            fam = (tbl, first)
            if cap > 0 and fam_cnt[fam] >= cap:
                continue
            fam_cnt[fam] += 1
            picked.append(idx)

        # 4) Ensure per-table coverage (keep at least 1 per table if possible).
        tbl2best: Dict[str, IndexKey] = {}
        for idx, s, b in scored:
            tbl = idx[0]
            if tbl not in tbl2best:
                tbl2best[tbl] = idx

        picked_set = set(picked)
        for tbl, best in tbl2best.items():
            if tbl not in {i[0] for i in picked_set}:
                picked_set.add(best)
                picked.append(best)

        return picked_set, picked

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
        appearing_full = set(appearing)
        eval_set = set(appearing_full)
        eval_order: List[IndexKey] = []

        # Optional WDCG pruning/order (Phase 0.5).
        if self.wdcg_enabled:
            try:
                eval_set, eval_order = self._wdcg_prepare_eval(workload, appearing_full)
            except Exception:
                eval_set, eval_order = set(appearing_full), []

        # For stats, we count the candidates that will actually be considered in this round.
        self._m_stats["candidate_count"] += len(eval_set)

        if not eval_set:
            return

        budget = len(eval_set) if self.workload_count == 0 else max(
            1, int(self.optimizer_ratio * len(eval_set))
        )
        trials = 0
        # Record the priority order considered for evaluation.
        # If WDCG returned an order, we use it (after pruning). Otherwise fall back to benefit order.
        for k in eval_set:
            self.columns_benefit.setdefault(k, 0.0)

        if eval_order:
            self._last_eval_order = [k for k in eval_order if k in eval_set]
        else:
            self._last_eval_order = [
                idx_key
                for idx_key, _ in sorted(
                    self.columns_benefit.items(), key=lambda kv: kv[1], reverse=True
                )
                if idx_key in eval_set
            ]
            # Append unseen candidates deterministically (benefit=0)
            for k in sorted(eval_set):
                if k not in self._last_eval_order:
                    self._last_eval_order.append(k)
        for idx_key in self._last_eval_order:
            if trials >= budget:
                break
            if idx_key not in eval_set:
                continue
            self._test_candidate(
                idx_key, query_indexes, base_costs, base_total, old_conf, workload
            )
            self._last_evaluated_set.add(idx_key)
            trials += 1


        self._m_stats["evaluated_count"] += trials
        decay_set = eval_set if self.wdcg_enabled else appearing_full
        for key in list(self.columns_benefit.keys()):
            if key not in decay_set:
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