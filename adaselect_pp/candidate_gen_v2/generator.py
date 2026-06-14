from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from adaselect_pp.common import norm_name, unique_keep_order
from .sql_evidence import StaticSQLExtractor
from .types import Candidate, GenerationResult, IndexKey, QueryEvidence, SeedState
from .vocabulary import ColumnVocabulary

logger = logging.getLogger(__name__)


class MCIGCandidateGenerator:
    """Predicate-first, prefix-growth multi-column candidate generator.

    This module is deliberately independent of EXPLAIN plans.  It uses only
    static SQL evidence, schema, PK/UNIQUE metadata, and an optional benchmark
    indexable-column whitelist.
    """

    FAMILY_SCORE = {
        "EQ_RANGE": 4.5,
        "EQ_EQ": 4.0,
        "EQ1": 3.0,
        "JOIN_EQ1": 2.4,
        "RANGE1": 2.0,
        "VACUUM_RESCUE1": 1.4,
    }
    SOURCE_SCORE = {
        "STRONG_AST": 1.0,
        "AST": 0.7,
        "STATIC_FALLBACK": 0.25,
        "VACUUM_RESCUE": 0.15,
    }

    def __init__(
        self,
        benchmark: str,
        db_con,
        *,
        max_width: int = 2,
        max_num: int = 40,
        indexable_path: str = "",
        per_query_cap: int = 12,
        per_table_cap: int = 4,
        round_table_cap: int = 6,
    ) -> None:
        self.benchmark = benchmark
        self.db = db_con
        self.max_width = int(max_width)
        if self.max_width > 2:
            raise ValueError("Phase 0.5 AdaSelect-PG supports max_width <= 2 only")
        self.max_num = int(max_num)
        self.per_query_cap = int(per_query_cap)
        self.per_table_cap = int(per_table_cap)
        self.round_table_cap = int(round_table_cap)
        self.probe_rounds = 2
        self.vocab = ColumnVocabulary.load(
            benchmark,
            db_con=db_con,
            explicit_path=indexable_path or "",
            required=True,
        )
        self.extractor = StaticSQLExtractor(db_con, self.vocab)
        self.pkuniq = self._load_pkuniq()
        self.last_meta: Dict[IndexKey, Dict[str, object]] = {}
        self.last_pair_supply: Dict[str, Set[IndexKey]] = {}
        # TraceRecorder compatibility: old code expects generator.enum.last_meta.
        self.enum = self
        logger.info(
            "CandidateGenerator init | class=%s benchmark=%s max_width=%d max_num=%d sqlglot_available=%s "
            "whitelist_path=%s whitelist_enabled=%s whitelist_tables=%d whitelist_columns=%d",
            self.__class__.__name__,
            self.benchmark,
            self.max_width,
            self.max_num,
            self.extractor.sqlglot_available,
            self.vocab.path,
            self.vocab.enabled,
            len(self.vocab.mapping),
            sum(len(cols) for cols in self.vocab.mapping.values()),
        )

    def _load_pkuniq(self) -> Set[IndexKey]:
        out: Set[IndexKey] = set()
        sql = """
        SELECT lower(t.relname), array_agg(lower(a.attname) ORDER BY x.ord)
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = x.attnum
        WHERE t.relkind = 'r'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND (i.indisprimary OR i.indisunique)
          AND i.indpred IS NULL
          AND i.indexprs IS NULL
          AND x.attnum > 0
          AND x.ord <= i.indnkeyatts
        GROUP BY t.relname, i.indexrelid
        """
        try:
            for table, cols in self.db.exec_fetchall(sql) or []:
                cc = tuple(norm_name(c) for c in (cols or []) if norm_name(c))
                if table and cc:
                    out.add((norm_name(table), cc))
        except Exception as exc:
            logger.warning("Failed to load PK/UNIQUE metadata: %s", exc)
        return out

    def _is_fixed(self, key: IndexKey) -> bool:
        return key in self.pkuniq

    def _emit(
        self,
        out: Dict[IndexKey, Candidate],
        *,
        query_id: int,
        template_id: str,
        table: str,
        cols: Sequence[str],
        family: str,
        source: str,
        roles: Sequence[str] = (),
        confidence: float = 0.7,
    ) -> None:
        t = norm_name(table)
        ctuple = tuple(norm_name(c) for c in cols if norm_name(c))
        if not t or not ctuple:
            return
        if len(ctuple) < 1 or len(ctuple) > self.max_width:
            return
        # Do not emit columns outside whitelist.
        if any(not self.vocab.is_allowed(t, c) for c in ctuple):
            return
        key: IndexKey = (t, ctuple)
        if self._is_fixed(key):
            return
        cand = out.get(key)
        if cand is None:
            cand = Candidate(key=key, family=family, source=source, confidence=float(confidence), roles=tuple(roles))
            out[key] = cand
        cand.query_ids.add(int(query_id))
        cand.template_ids.add(str(template_id))
        cand.support_count = len(cand.query_ids)

    def _score(self, cand: Candidate) -> float:
        fam = self.FAMILY_SCORE.get(cand.family, 1.0)
        src = self.SOURCE_SCORE.get(cand.source, 0.0)
        width_penalty = 0.15 * max(0, len(cand.key[1]) - 1)
        return fam + src + 0.30 * cand.support_count + 0.20 * float(cand.confidence) - width_penalty

    def _query_sort_key(self, item: Tuple[IndexKey, Candidate]) -> Tuple[bool, float, IndexKey]:
        key, cand = item
        return (len(key[1]) > 1, -self._score(cand), key)

    @staticmethod
    def _candidate_sort_key(cand: Candidate) -> Tuple[bool, float, IndexKey]:
        return (len(cand.key[1]) > 1, -cand.score, cand.key)

    @staticmethod
    def _is_width2(key: IndexKey) -> bool:
        return isinstance(key, tuple) and len(key) == 2 and isinstance(key[1], tuple) and len(key[1]) == 2

    @staticmethod
    def _fmt_index_key(key: IndexKey) -> str:
        return f"{key[0]}({','.join(key[1])})"

    @classmethod
    def _serialize_examples(cls, keys: Iterable[IndexKey], limit: int = 8) -> str:
        return ";".join(cls._fmt_index_key(k) for k in sorted(set(keys))[:limit])

    @staticmethod
    def _serialize_by_table(counter: Counter) -> str:
        return "|".join(f"{table}:{int(count)}" for table, count in sorted(counter.items()) if int(count) > 0)

    def _best_eq_cols(self, evidence: QueryEvidence, table: str) -> List[str]:
        # Deterministic, conservative: filter EQ before join EQ.
        return unique_keep_order((evidence.filter_eq.get(table, []) or []) + (evidence.join_eq.get(table, []) or []))

    def _extract_evidence(self, workload_lines: Sequence[str]) -> Tuple[List[QueryEvidence], Counter]:
        evidences: List[QueryEvidence] = []
        parse_status = Counter()
        for qid, line in enumerate(workload_lines):
            evidence = self.extractor.extract_line(line, qid)
            parse_status[evidence.parse_status] += 1
            evidences.append(evidence)
        return evidences, parse_status

    def _emit_single_probes(self, evidence: QueryEvidence) -> Dict[IndexKey, Candidate]:
        out: Dict[IndexKey, Candidate] = {}
        source = "AST" if evidence.parse_status == "ast_ok" else "STATIC_FALLBACK"

        for table, cols in evidence.filter_eq.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family="EQ1", source=source, roles=("filter_eq",), confidence=0.85)
        for table, cols in evidence.join_eq.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family="JOIN_EQ1", source=source, roles=("join_eq",), confidence=0.65)
        for table, cols in evidence.filter_rng.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family="RANGE1", source=source, roles=("range",), confidence=0.65)
        return out

    def _add_vacuum_rescue(self, evidence: QueryEvidence, out: Dict[IndexKey, Candidate]) -> None:
        present_tables = {key[0] for key in out}
        for table in sorted(evidence.tables):
            if table in present_tables:
                continue
            evidence_cols = unique_keep_order(
                (evidence.filter_eq.get(table, []) or [])
                + (evidence.join_eq.get(table, []) or [])
                + (evidence.filter_rng.get(table, []) or [])
            )
            for col in evidence_cols:
                key = (table, (col,))
                if self._is_fixed(key):
                    continue
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family="VACUUM_RESCUE1", source="VACUUM_RESCUE", roles=("rescue",), confidence=0.50)
                break

    def _query_reduce_with_diagnostics(
        self,
        out: Dict[IndexKey, Candidate],
        *,
        pair_supply_ceiling_enabled: bool = False,
    ) -> Tuple[Dict[IndexKey, Candidate], Dict[str, Any]]:
        before_width2 = {key for key in out if self._is_width2(key)}
        table_counts: Dict[str, int] = defaultdict(int)
        selected: Dict[IndexKey, Candidate] = {}
        for key, cand in sorted(out.items(), key=self._query_sort_key):
            if table_counts[key[0]] >= self.per_table_cap:
                continue
            selected[key] = cand
            table_counts[key[0]] += 1
            if len(selected) >= self.per_query_cap:
                break
        normal_width2 = {key for key in selected if self._is_width2(key)}
        dropped_width2 = before_width2 - normal_width2
        ceiling_added = set()
        if pair_supply_ceiling_enabled:
            for key, cand in sorted(out.items(), key=self._query_sort_key):
                if key in selected or not self._is_width2(key):
                    continue
                selected[key] = cand
                ceiling_added.add(key)
        after_width2 = {key for key in selected if self._is_width2(key)}
        return selected, {
            "width2_before": before_width2,
            "width2_after": after_width2,
            "width2_dropped": dropped_width2,
            "ceiling_added_width2": ceiling_added,
        }

    def _query_reduce(self, out: Dict[IndexKey, Candidate]) -> Dict[IndexKey, Candidate]:
        selected, _diag = self._query_reduce_with_diagnostics(out)
        return selected

    def _round_select_with_diagnostics(
        self,
        merged: Dict[IndexKey, Candidate],
        topk: int,
        *,
        pair_supply_ceiling_enabled: bool = False,
    ) -> Tuple[List[Candidate], Dict[str, Any]]:
        before_width2 = {key for key in merged if self._is_width2(key)}
        ranked = sorted(merged.values(), key=self._candidate_sort_key)
        table_counts: Dict[str, int] = defaultdict(int)
        selected: List[Candidate] = []
        limit = max(1, int(topk))
        for cand in ranked:
            if len(selected) >= limit:
                break
            if table_counts[cand.key[0]] >= self.round_table_cap:
                continue
            selected.append(cand)
            table_counts[cand.key[0]] += 1
        normal_keys = {cand.key for cand in selected}
        normal_width2 = {cand.key for cand in selected if self._is_width2(cand.key)}
        dropped_width2 = before_width2 - normal_width2
        ceiling_added = set()
        if pair_supply_ceiling_enabled:
            for cand in ranked:
                if cand.key in normal_keys or not self._is_width2(cand.key):
                    continue
                selected.append(cand)
                normal_keys.add(cand.key)
                ceiling_added.add(cand.key)
        after_width2 = {cand.key for cand in selected if self._is_width2(cand.key)}
        best_width2_family_score = 0.0
        width1_ahead = 0
        max_displacing_width1_family_score = 0.0
        width2_candidates = [cand for cand in merged.values() if self._is_width2(cand.key)]
        if width2_candidates:
            best_width2 = sorted(width2_candidates, key=lambda cand: (-float(cand.score), cand.key))[0]
            best_width2_family_score = float(self.FAMILY_SCORE.get(best_width2.family, 0.0))
            for cand in ranked:
                if cand.key == best_width2.key:
                    break
                if len(cand.key[1]) == 1:
                    width1_ahead += 1
                    max_displacing_width1_family_score = max(
                        max_displacing_width1_family_score,
                        float(self.FAMILY_SCORE.get(cand.family, 0.0)),
                    )
        return selected, {
            "width2_before": before_width2,
            "width2_after": after_width2,
            "width2_dropped": dropped_width2,
            "ceiling_added_width2": ceiling_added,
            "width1_ranked_ahead_of_best_width2": int(width1_ahead),
            "best_width2_family_score": float(best_width2_family_score),
            "max_family_score_of_displacing_width1": float(max_displacing_width1_family_score),
        }

    def _make_seed_states(
        self,
        *,
        seed_benefit: Optional[Dict[IndexKey, float]] = None,
        seed_seen_count: Optional[Dict[IndexKey, int]] = None,
        seed_positive_count: Optional[Dict[IndexKey, int]] = None,
        seed_last_obs_src: Optional[Dict[IndexKey, str]] = None,
        seed_first_seen_round: Optional[Dict[IndexKey, int]] = None,
        seed_last_seen_round: Optional[Dict[IndexKey, int]] = None,
        seed_seen_rounds: Optional[Dict[IndexKey, Set[int]]] = None,
        seed_normalized_benefit: Optional[Dict[IndexKey, float]] = None,
    ) -> Dict[IndexKey, SeedState]:
        keys = set(seed_benefit or {}) | set(seed_seen_count or {}) | set(seed_positive_count or {})
        out: Dict[IndexKey, SeedState] = {}
        for key in keys:
            benefit = float((seed_benefit or {}).get(key, 0.0) or 0.0)
            seen = int((seed_seen_count or {}).get(key, 0) or 0)
            positive = int((seed_positive_count or {}).get(key, 0) or 0)
            last_src = str((seed_last_obs_src or {}).get(key, "") or "")
            mature = seen > 0 and positive > 0 and benefit > 0.0 and last_src not in {"NO_HIT", "ALL_FALLBACK"}
            out[key] = SeedState(
                key=key,
                first_seen_round=int((seed_first_seen_round or {}).get(key, 0) or 0),
                last_seen_round=int((seed_last_seen_round or {}).get(key, 0) or 0),
                seen_rounds=set((seed_seen_rounds or {}).get(key, set()) or set()),
                evaluated_count=seen,
                positive_count=positive,
                benefit=benefit,
                normalized_benefit=float((seed_normalized_benefit or {}).get(key, 0.0) or 0.0),
                last_obs_src=last_src,
                mature=mature,
            )
        return out

    def _grow_width2(
        self,
        evidence: QueryEvidence,
        singles: Dict[IndexKey, Candidate],
        seed_states: Dict[IndexKey, SeedState],
        rejected: Counter,
        grow_meta: Dict[IndexKey, Dict[str, object]],
    ) -> Dict[IndexKey, Candidate]:
        out: Dict[IndexKey, Candidate] = {}
        source = "AST" if evidence.parse_status == "ast_ok" else "STATIC_FALLBACK"
        if evidence.parse_status != "ast_ok":
            rejected["rejected_growth_parse_fallback"] += 1
            return out
        if evidence.has_or:
            rejected["rejected_growth_has_or"] += 1
            return out
        for table in sorted(evidence.tables):
            if table in evidence.alias_ambiguous_tables:
                rejected["rejected_growth_alias_ambiguous"] += 1
                continue
            eq_cols = self._best_eq_cols(evidence, table)
            rng_cols = unique_keep_order(evidence.filter_rng.get(table, []) or [])
            for seed_col in eq_cols:
                seed_key = (table, (seed_col,))
                seed_cand = singles.get(seed_key)
                seed_state = seed_states.get(seed_key)
                if seed_cand is None:
                    continue
                if seed_cand.family == "RANGE1":
                    rejected["rejected_growth_range_seed"] += 1
                    continue
                if seed_state is None or seed_state.evaluated_count <= 0:
                    rejected["rejected_growth_seed_unseen"] += 1
                    continue
                if not seed_state.mature:
                    rejected["rejected_growth_seed_not_positive"] += 1
                    continue
                for col in eq_cols:
                    if col == seed_col:
                        continue
                    key = (table, (seed_col, col))
                    self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(seed_col, col), family="EQ_EQ", source=source, roles=("seed_eq", "eq"), confidence=0.90)
                    if key in out:
                        self._record_grow_meta(grow_meta, key, seed_state, "seed_eq_plus_eq", seed_cand.family)
                for col in rng_cols:
                    if col == seed_col:
                        continue
                    key = (table, (seed_col, col))
                    self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(seed_col, col), family="EQ_RANGE", source=source, roles=("seed_eq", "range"), confidence=0.85)
                    if key in out:
                        self._record_grow_meta(grow_meta, key, seed_state, "seed_eq_plus_range", seed_cand.family)
        return out

    @staticmethod
    def _seed_meta(seed: SeedState, grow_reason: str, seed_family: str = "") -> Dict[str, object]:
        return {
            "seed_key": seed.key,
            "grow_seed_key": seed.key,
            "grow_seed_family": seed_family,
            "grow_seed_family_set": [seed_family] if seed_family else [],
            "seed_benefit": seed.benefit,
            "seed_normalized_benefit": seed.normalized_benefit,
            "seed_evaluated_count": seed.evaluated_count,
            "seed_positive_count": seed.positive_count,
            "seed_first_seen_round": seed.first_seen_round,
            "seed_last_seen_round": seed.last_seen_round,
            "seed_seen_rounds": sorted(seed.seen_rounds),
            "seed_last_obs_src": seed.last_obs_src,
            "seed_mature": seed.mature,
            "grow_reason": grow_reason,
            "rejected_growth_reason": "",
        }

    @classmethod
    def _record_grow_meta(
        cls,
        grow_meta: Dict[IndexKey, Dict[str, object]],
        key: IndexKey,
        seed: SeedState,
        grow_reason: str,
        seed_family: str,
    ) -> None:
        meta = grow_meta.get(key)
        if meta is None:
            grow_meta[key] = cls._seed_meta(seed, grow_reason, seed_family)
            return
        families = set(str(x) for x in meta.get("grow_seed_family_set", []) if str(x))
        if seed_family:
            families.add(str(seed_family))
        meta["grow_seed_family_set"] = sorted(families)
        if not meta.get("grow_seed_family") and seed_family:
            meta["grow_seed_family"] = seed_family
        if not meta.get("grow_seed_key"):
            meta["grow_seed_key"] = seed.key

    def _candidate_meta(self, cand: Candidate, *, diagnostic_only: bool = False) -> Dict[str, object]:
        meta: Dict[str, object] = {
            "family": cand.family,
            "source": cand.source,
            "confidence": cand.confidence,
            "support_count": cand.support_count,
            "score": cand.score,
            "roles": list(cand.roles),
            "width_before_merge": len(cand.key[1]),
            "width_after_merge": len(cand.key[1]),
        }
        if diagnostic_only:
            meta["diagnostic_only"] = 1
        return meta

    @staticmethod
    def _as_tuple_index(value: Any) -> Optional[IndexKey]:
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], tuple):
            return value
        return None

    def _annotate_pair_fidelity(self, meta_map: Dict[IndexKey, Dict[str, object]]) -> Dict[str, int]:
        mismatch = 0
        seed_missing = 0
        join_downgraded = 0
        for key, meta in list(meta_map.items()):
            if not self._is_width2(key) or not isinstance(meta, dict):
                continue
            seed_key = self._as_tuple_index(meta.get("grow_seed_key", meta.get("seed_key", None)))
            seed_meta = meta_map.get(seed_key, {}) if seed_key is not None and isinstance(meta_map, dict) else {}
            seed_family = str(meta.get("grow_seed_family", "") or "")
            if not seed_family and isinstance(seed_meta, dict):
                seed_family = str(seed_meta.get("family", "") or "")
            seed_families = {str(x) for x in meta.get("grow_seed_family_set", []) if str(x)}
            if seed_family:
                seed_families.add(seed_family)
            grow_reason = str(meta.get("grow_reason", "") or "")
            if grow_reason and not seed_families:
                seed_missing += 1
                meta["seed_family_missing"] = 1
            else:
                meta["seed_family_missing"] = 0
            family = str(meta.get("family", "") or "")
            join_seeded = any(fam.startswith("JOIN") for fam in seed_families)
            expected_join_family = ""
            if join_seeded and family == "EQ_RANGE":
                expected_join_family = "JOIN_RANGE"
            elif join_seeded and family == "EQ_EQ":
                expected_join_family = "JOIN_EQ"
            if expected_join_family:
                mismatch += 1
                join_downgraded += 1
                meta["pair_family_vs_grow_reason_mismatch"] = 1
                meta["expected_structural_pair_type"] = expected_join_family
            else:
                meta["pair_family_vs_grow_reason_mismatch"] = 0
            meta["join_seed_downgraded"] = int(bool(expected_join_family))
            if grow_reason and seed_families and family not in {"EQ_RANGE", "EQ_EQ", "JOIN_RANGE", "JOIN_EQ"}:
                meta["pair_family_vs_grow_reason_mismatch"] = 1
                mismatch += 1
        return {
            "pair_family_vs_grow_reason_mismatch": int(mismatch),
            "seed_family_missing_count": int(seed_missing),
            "join_seed_downgraded_count": int(join_downgraded),
        }

    def generate(
        self,
        workload_lines: Sequence[str],
        *,
        old_conf: Optional[Set[IndexKey]] = None,
        mu_table: Optional[Dict[IndexKey, float]] = None,
        topk: int = 40,
        workload_count: int = 0,
        seed_benefit: Optional[Dict[IndexKey, float]] = None,
        seed_seen_count: Optional[Dict[IndexKey, int]] = None,
        seed_positive_count: Optional[Dict[IndexKey, int]] = None,
        seed_last_obs_src: Optional[Dict[IndexKey, str]] = None,
        seed_first_seen_round: Optional[Dict[IndexKey, int]] = None,
        seed_last_seen_round: Optional[Dict[IndexKey, int]] = None,
        seed_seen_rounds: Optional[Dict[IndexKey, Set[int]]] = None,
        seed_normalized_benefit: Optional[Dict[IndexKey, float]] = None,
        pair_supply_ceiling_enabled: bool = False,
        target_pair_audit: Optional[Set[IndexKey]] = None,
        **_ignored,
    ) -> GenerationResult:
        start = time.perf_counter()
        per_query: List[Set[IndexKey]] = []
        merged: Dict[IndexKey, Candidate] = {}
        family_raw = Counter()
        source_raw = Counter()
        rejected = Counter()
        grow_meta: Dict[IndexKey, Dict[str, object]] = {}
        diagnostic_width2: Dict[IndexKey, Candidate] = {}
        perquery_width2_before: Set[IndexKey] = set()
        perquery_width2_after: Set[IndexKey] = set()
        perquery_width2_dropped: Set[IndexKey] = set()
        perquery_width2_before_events = 0
        perquery_width2_after_events = 0
        perquery_width2_dropped_events = 0
        perquery_width2_ceiling_added_events = 0
        perquery_width2_ceiling_added: Set[IndexKey] = set()
        perquery_dropped_by_table = Counter()
        pair_supply_ceiling_enabled = bool(pair_supply_ceiling_enabled)
        target_pairs = set(target_pair_audit or set())
        evidences, parse_status = self._extract_evidence(workload_lines)
        seed_states = self._make_seed_states(
            seed_benefit=seed_benefit or mu_table,
            seed_seen_count=seed_seen_count,
            seed_positive_count=seed_positive_count,
            seed_last_obs_src=seed_last_obs_src,
            seed_first_seen_round=seed_first_seen_round,
            seed_last_seen_round=seed_last_seen_round,
            seed_seen_rounds=seed_seen_rounds,
            seed_normalized_benefit=seed_normalized_benefit,
        )
        gen_mode = "probe" if int(workload_count) < self.probe_rounds else "grow"

        for evidence in evidences:
            qmap = self._emit_single_probes(evidence)
            if gen_mode == "grow":
                qmap.update(self._grow_width2(evidence, qmap, seed_states, rejected, grow_meta))
            self._add_vacuum_rescue(evidence, qmap)
            for key, cand in qmap.items():
                if self._is_width2(key) and key not in diagnostic_width2:
                    diagnostic_width2[key] = cand
            qmap, query_diag = self._query_reduce_with_diagnostics(
                qmap,
                pair_supply_ceiling_enabled=pair_supply_ceiling_enabled,
            )
            query_width2_before = set(query_diag.get("width2_before", set()) or set())
            query_width2_after = set(query_diag.get("width2_after", set()) or set())
            query_width2_dropped = set(query_diag.get("width2_dropped", set()) or set())
            query_width2_ceiling_added = set(query_diag.get("ceiling_added_width2", set()) or set())
            perquery_width2_before |= query_width2_before
            perquery_width2_after |= query_width2_after
            perquery_width2_dropped |= query_width2_dropped
            perquery_width2_ceiling_added |= query_width2_ceiling_added
            perquery_width2_before_events += len(query_width2_before)
            perquery_width2_after_events += len(query_width2_after)
            perquery_width2_dropped_events += len(query_width2_dropped)
            perquery_width2_ceiling_added_events += len(query_width2_ceiling_added)
            perquery_dropped_by_table.update(key[0] for key in query_width2_dropped)
            qset = set(qmap)
            per_query.append(qset)
            for key, cand in qmap.items():
                family_raw[cand.family] += 1
                source_raw[cand.source] += 1
                existing = merged.get(key)
                if existing is None:
                    merged[key] = cand
                else:
                    existing.query_ids |= cand.query_ids
                    existing.template_ids |= cand.template_ids
                    existing.support_count = len(existing.query_ids)
                    # Keep the stronger family/source if duplicate evidence appears.
                    if self.FAMILY_SCORE.get(cand.family, 0) > self.FAMILY_SCORE.get(existing.family, 0):
                        existing.family = cand.family
                        existing.source = cand.source
                        existing.roles = cand.roles
                        existing.confidence = max(existing.confidence, cand.confidence)
                    if key in grow_meta:
                        grow_meta[key]["support_query_ids"] = sorted(existing.query_ids)

        for cand in merged.values():
            cand.score = self._score(cand)

        for cand in diagnostic_width2.values():
            cand.score = self._score(cand)

        selected, round_diag = self._round_select_with_diagnostics(
            merged,
            topk,
            pair_supply_ceiling_enabled=pair_supply_ceiling_enabled,
        )
        round_width2_before = set(round_diag.get("width2_before", set()) or set())
        round_width2_after = set(round_diag.get("width2_after", set()) or set())
        round_width2_dropped = set(round_diag.get("width2_dropped", set()) or set())
        round_width2_ceiling_added = set(round_diag.get("ceiling_added_width2", set()) or set())
        round_dropped_by_table = Counter(key[0] for key in round_width2_dropped)
        ceiling_added_any = perquery_width2_ceiling_added | round_width2_ceiling_added

        topk_set = {c.key for c in selected}
        candidate_count_delta = len(topk_set & ceiling_added_any)
        recovered_targets = target_pairs & topk_set & ceiling_added_any
        score_map = {c.key: c.score for c in selected}
        meta_map: Dict[IndexKey, Dict[str, object]] = {}
        for key, cand in merged.items():
            meta_map[key] = self._candidate_meta(cand)
            if len(key[1]) == 2:
                meta_map[key].update(grow_meta.get(key, {"rejected_growth_reason": "missing_seed_provenance"}))
        for key, cand in diagnostic_width2.items():
            if key in meta_map:
                continue
            meta_map[key] = self._candidate_meta(cand, diagnostic_only=True)
            meta_map[key].update(grow_meta.get(key, {"rejected_growth_reason": "missing_seed_provenance"}))
            meta_map[key]["dropped_perquery_cap"] = 1 if key in perquery_width2_dropped else 0
            meta_map[key]["dropped_round_cap"] = 0
        for key in perquery_width2_dropped:
            if key in meta_map:
                meta_map[key]["dropped_perquery_cap"] = 1
        for key in round_width2_dropped:
            if key in meta_map:
                meta_map[key]["dropped_round_cap"] = 1
        fidelity_stats = self._annotate_pair_fidelity(meta_map)
        self.last_meta = dict(meta_map)
        self.last_pair_supply = {
            "prequery_width2": set(perquery_width2_before),
            "postquery_width2": set(perquery_width2_after),
            "dropped_perquery_width2": set(perquery_width2_dropped),
            "preround_width2": set(round_width2_before),
            "postround_width2": set(round_width2_after),
            "dropped_round_width2": set(round_width2_dropped),
            "ceiling_added_perquery_width2": set(perquery_width2_ceiling_added),
            "ceiling_added_round_width2": set(round_width2_ceiling_added),
        }

        aff = [sum(1 for qset in per_query if key in qset) for key in topk_set]
        stats = {
            "candidate_count_raw": len(merged),
            "gen_mode": gen_mode,
            "probe_rounds": self.probe_rounds,
            "workload_count": int(workload_count),
            "wdcg_pruned_count": len(topk_set),
            "wdcg_selected_post_compile": len(topk_set),
            "merged_total": 0,
            "merged_group": 0,
            "merged_order": 0,
            "merged_covering": 0,
            "compile_validation_enabled": False,
            "compile_validation_trials": 0,
            "compile_validated": 0,
            "compile_invalidated": 0,
            "compile_errors": 0,
            "compile_not_picked": 0,
            "parse_ast_ok": int(parse_status.get("ast_ok", 0)),
            "parse_fallback_regex": int(parse_status.get("fallback_regex", 0)),
            "family_eq1": int(family_raw.get("EQ1", 0)),
            "family_join_eq1": int(family_raw.get("JOIN_EQ1", 0)),
            "family_range1": int(family_raw.get("RANGE1", 0)),
            "family_eqeq": int(family_raw.get("EQ_EQ", 0)),
            "family_eqrange": int(family_raw.get("EQ_RANGE", 0)),
            "family_rescue": int(family_raw.get("VACUUM_RESCUE1", 0)),
            "width1_count": sum(1 for k in merged if len(k[1]) == 1),
            "width2_count": sum(1 for k in merged if len(k[1]) == 2),
            "width2_candidates_perquery_before_cap": int(perquery_width2_before_events),
            "width2_candidates_perquery_after_cap": int(perquery_width2_after_events),
            "width2_cap_dropped_perquery_events": int(perquery_width2_dropped_events),
            "width2_cap_dropped_perquery_by_table": self._serialize_by_table(perquery_dropped_by_table),
            "width2_cap_dropped_perquery_examples": self._serialize_examples(perquery_width2_dropped),
            "width2_candidates_round_before_cap": int(len(round_width2_before)),
            "width2_candidates_round_after_cap": int(len(round_width2_after)),
            "width2_cap_dropped_round": int(len(round_width2_dropped)),
            "width2_cap_dropped_round_by_table": self._serialize_by_table(round_dropped_by_table),
            "width2_cap_dropped_round_examples": self._serialize_examples(round_width2_dropped),
            "pair_supply_ceiling_enabled": int(pair_supply_ceiling_enabled),
            "pair_supply_ceiling_width2_added_perquery": int(perquery_width2_ceiling_added_events),
            "pair_supply_ceiling_width2_added_round": int(len(round_width2_ceiling_added)),
            "pair_supply_ceiling_width2_survived_count": int(len(round_width2_after)),
            "pair_supply_ceiling_target_pairs_recovered": int(len(recovered_targets)),
            "pair_supply_ceiling_candidate_count_delta": int(candidate_count_delta),
            "pair_supply_ceiling_examples": self._serialize_examples(topk_set & ceiling_added_any),
            "width1_ranked_ahead_of_best_width2": int(round_diag.get("width1_ranked_ahead_of_best_width2", 0)),
            "best_width2_family_score": float(round_diag.get("best_width2_family_score", 0.0)),
            "max_family_score_of_displacing_width1": float(round_diag.get("max_family_score_of_displacing_width1", 0.0)),
            "pair_family_vs_grow_reason_mismatch": int(fidelity_stats.get("pair_family_vs_grow_reason_mismatch", 0)),
            "seed_family_missing_count": int(fidelity_stats.get("seed_family_missing_count", 0)),
            "join_seed_downgraded_count": int(fidelity_stats.get("join_seed_downgraded_count", 0)),
            "seed_count": sum(1 for s in seed_states.values() if len(s.key[1]) == 1),
            "eligible_seed_count": sum(1 for s in seed_states.values() if len(s.key[1]) == 1 and s.mature),
            "multi_growth_count": sum(1 for k in merged if len(k[1]) == 2),
            "rejected_growth_has_or": int(rejected.get("rejected_growth_has_or", 0)),
            "rejected_growth_alias_ambiguous": int(rejected.get("rejected_growth_alias_ambiguous", 0)),
            "rejected_growth_seed_not_positive": int(rejected.get("rejected_growth_seed_not_positive", 0)),
            "rejected_growth_seed_unseen": int(rejected.get("rejected_growth_seed_unseen", 0)),
            "rejected_growth_range_seed": int(rejected.get("rejected_growth_range_seed", 0)),
            "rejected_growth_parse_fallback": int(rejected.get("rejected_growth_parse_fallback", 0)),
            "source_ast": int(source_raw.get("AST", 0)),
            "source_strong_ast": int(source_raw.get("STRONG_AST", 0)),
            "source_static_fallback": int(source_raw.get("STATIC_FALLBACK", 0)),
            "source_vacuum_rescue": int(source_raw.get("VACUUM_RESCUE", 0)),
            "vocab_enabled": int(self.vocab.enabled),
            "vocab_path": self.vocab.path,
            "vocab_tables": len(self.vocab.mapping),
            "vocab_columns": sum(len(cols) for cols in self.vocab.mapping.values()),
            "sqlglot_available": int(self.extractor.sqlglot_available),
            "raw_benefit_in_generator_score": False,
            "wdcg_elapsed_ms": (time.perf_counter() - start) * 1000.0,
        }
        if aff:
            sorted_aff = sorted(aff)
            stats.update({
                "aff_avg": sum(aff) / len(aff),
                "aff_p90": sorted_aff[int(0.9 * (len(sorted_aff) - 1))],
                "aff_max": max(aff),
                "predicted_what_if_calls": sum(aff),
            })
        return GenerationResult(per_query, topk_set, score_map, meta_map, stats)
