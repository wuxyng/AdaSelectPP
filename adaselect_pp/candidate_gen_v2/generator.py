from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from adaselect_pp.common import norm_name, unique_keep_order
from .sql_evidence import StaticSQLExtractor
from .types import Candidate, GenerationResult, IndexKey, QueryEvidence
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
        self.vocab = ColumnVocabulary.load(
            benchmark,
            db_con=db_con,
            explicit_path=indexable_path or "",
            required=True,
        )
        self.extractor = StaticSQLExtractor(db_con, self.vocab)
        self.pkuniq = self._load_pkuniq()
        self.last_meta: Dict[IndexKey, Dict[str, object]] = {}
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

    def _best_eq_cols(self, evidence: QueryEvidence, table: str) -> List[str]:
        # Deterministic, conservative: filter EQ before join EQ.
        return unique_keep_order((evidence.filter_eq.get(table, []) or []) + (evidence.join_eq.get(table, []) or []))

    def _build_query_candidates(self, evidence: QueryEvidence) -> Dict[IndexKey, Candidate]:
        out: Dict[IndexKey, Candidate] = {}
        source = "AST" if evidence.parse_status == "ast_ok" else "STATIC_FALLBACK"

        # Single-column seeds.
        for table, cols in evidence.filter_eq.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family="EQ1", source=source, roles=("filter_eq",), confidence=0.85)
        for table, cols in evidence.join_eq.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family="JOIN_EQ1", source=source, roles=("join_eq",), confidence=0.65)
        for table, cols in evidence.filter_rng.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family="RANGE1", source=source, roles=("range",), confidence=0.65)

        # Strong AST EQ_EQ: only top two filter equality columns in a non-OR factor.
        if self.max_width >= 2 and evidence.parse_status == "ast_ok" and not evidence.has_or:
            for table, cols in evidence.strong_factor_eq.items():
                gcols = unique_keep_order(cols)[:2]
                if len(gcols) == 2:
                    self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=gcols, family="EQ_EQ", source="STRONG_AST", roles=("filter_eq", "filter_eq"), confidence=0.95)

        # EQ_RANGE / JOIN_RANGE-style prefix: best equality-like col + best range col.
        if self.max_width >= 2:
            for table in sorted(evidence.tables):
                eq_cols = self._best_eq_cols(evidence, table)
                rng_cols = unique_keep_order(evidence.filter_rng.get(table, []) or [])
                if eq_cols and rng_cols and eq_cols[0] != rng_cols[0]:
                    self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(eq_cols[0], rng_cols[0]), family="EQ_RANGE", source=source, roles=("eq", "range"), confidence=0.80)

        # Candidate-vacuum rescue: if a table has evidence but no viable candidate
        # after PK/UNIQUE filtering, add one non-fixed single-column fallback.
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

        # Query-local reducer.
        table_counts: Dict[str, int] = defaultdict(int)
        selected: Dict[IndexKey, Candidate] = {}
        for key, cand in sorted(out.items(), key=lambda kv: (-self._score(kv[1]), kv[0])):
            if table_counts[key[0]] >= self.per_table_cap:
                continue
            selected[key] = cand
            table_counts[key[0]] += 1
            if len(selected) >= self.per_query_cap:
                break
        return selected

    def generate(
        self,
        workload_lines: Sequence[str],
        *,
        old_conf: Optional[Set[IndexKey]] = None,
        mu_table: Optional[Dict[IndexKey, float]] = None,
        topk: int = 40,
        **_ignored,
    ) -> GenerationResult:
        start = time.perf_counter()
        per_query: List[Set[IndexKey]] = []
        merged: Dict[IndexKey, Candidate] = {}
        parse_status = Counter()
        family_raw = Counter()
        source_raw = Counter()

        for qid, line in enumerate(workload_lines):
            evidence = self.extractor.extract_line(line, qid)
            parse_status[evidence.parse_status] += 1
            qmap = self._build_query_candidates(evidence)
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

        for cand in merged.values():
            cand.score = self._score(cand)

        table_counts: Dict[str, int] = defaultdict(int)
        selected: List[Candidate] = []
        limit = max(1, int(topk))
        for cand in sorted(merged.values(), key=lambda c: (-c.score, c.key)):
            if len(selected) >= limit:
                break
            if table_counts[cand.key[0]] >= self.round_table_cap:
                continue
            selected.append(cand)
            table_counts[cand.key[0]] += 1

        topk_set = {c.key for c in selected}
        score_map = {c.key: c.score for c in selected}
        meta_map: Dict[IndexKey, Dict[str, object]] = {}
        for key, cand in merged.items():
            meta_map[key] = {
                "family": cand.family,
                "source": cand.source,
                "confidence": cand.confidence,
                "support_count": cand.support_count,
                "score": cand.score,
                "roles": list(cand.roles),
                "width_before_merge": len(key[1]),
                "width_after_merge": len(key[1]),
            }
        self.last_meta = dict(meta_map)

        aff = [sum(1 for qset in per_query if key in qset) for key in topk_set]
        stats = {
            "candidate_count_raw": len(merged),
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
