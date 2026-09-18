from __future__ import annotations
from adasel.policy import POLICY_DEFAULTS
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
    FAMILY_SCORE = POLICY_DEFAULTS['candidate_family_scores']
    SOURCE_SCORE = POLICY_DEFAULTS['candidate_source_scores']

    def __init__(self, benchmark: str, db_con, *, max_width: int=2, max_num: int=40, indexable_path: str='', per_query_cap: int=12, per_table_cap: int=4, round_table_cap: int=6) -> None:
        self.benchmark = benchmark
        self.db = db_con
        self.max_width = int(max_width)
        if self.max_width > 2:
            raise ValueError('Phase 0.5 AdaSelect-PG supports max_width <= 2 only')
        self.max_num = int(max_num)
        self.per_query_cap = int(per_query_cap)
        self.per_table_cap = int(per_table_cap)
        self.round_table_cap = int(round_table_cap)
        self.probe_rounds = POLICY_DEFAULTS['probe_rounds']
        self.vocab = ColumnVocabulary.load(benchmark, db_con=db_con, explicit_path=indexable_path or '', required=True)
        self.extractor = StaticSQLExtractor(db_con, self.vocab)
        self.pkuniq = self._load_pkuniq()
        self.last_meta: Dict[IndexKey, Dict[str, object]] = {}
        logger.info('CandidateGenerator init | class=%s benchmark=%s max_width=%d max_num=%d sqlglot_available=%s whitelist_path=%s whitelist_enabled=%s whitelist_tables=%d whitelist_columns=%d', self.__class__.__name__, self.benchmark, self.max_width, self.max_num, self.extractor.sqlglot_available, self.vocab.path, self.vocab.enabled, len(self.vocab.mapping), sum((len(cols) for cols in self.vocab.mapping.values())))

    def _load_pkuniq(self) -> Set[IndexKey]:
        out: Set[IndexKey] = set()
        sql = "\n        SELECT lower(t.relname), array_agg(lower(a.attname) ORDER BY x.ord)\n        FROM pg_index i\n        JOIN pg_class t ON t.oid = i.indrelid\n        JOIN pg_namespace n ON n.oid = t.relnamespace\n        JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ord) ON TRUE\n        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = x.attnum\n        WHERE t.relkind = 'r'\n          AND n.nspname NOT IN ('pg_catalog', 'information_schema')\n          AND (i.indisprimary OR i.indisunique)\n          AND i.indpred IS NULL\n          AND i.indexprs IS NULL\n          AND x.attnum > 0\n          AND x.ord <= i.indnkeyatts\n        GROUP BY t.relname, i.indexrelid\n        "
        try:
            for table, cols in self.db.exec_fetchall(sql) or []:
                cc = tuple((norm_name(c) for c in cols or [] if norm_name(c)))
                if table and cc:
                    out.add((norm_name(table), cc))
        except Exception as exc:
            logger.warning('Failed to load PK/UNIQUE metadata: %s', exc)
        return out

    def _is_fixed(self, key: IndexKey) -> bool:
        return key in self.pkuniq

    def _emit(self, out: Dict[IndexKey, Candidate], *, query_id: int, template_id: str, table: str, cols: Sequence[str], family: str, source: str, roles: Sequence[str]=(), confidence: float=POLICY_DEFAULTS['candidate_confidence_default']) -> None:
        t = norm_name(table)
        ctuple = tuple((norm_name(c) for c in cols if norm_name(c)))
        if not t or not ctuple:
            return
        if len(ctuple) < 1 or len(ctuple) > self.max_width:
            return
        if any((not self.vocab.is_allowed(t, c) for c in ctuple)):
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
        fam = self.FAMILY_SCORE.get(cand.family, POLICY_DEFAULTS['candidate_family_default'])
        src = self.SOURCE_SCORE.get(cand.source, 0.0)
        width_penalty = POLICY_DEFAULTS['candidate_width_penalty'] * max(0, len(cand.key[1]) - 1)
        return fam + src + POLICY_DEFAULTS['candidate_support_weight'] * cand.support_count + POLICY_DEFAULTS['candidate_confidence_weight'] * float(cand.confidence) - width_penalty

    def _query_sort_key(self, item: Tuple[IndexKey, Candidate]) -> Tuple[bool, float, IndexKey]:
        key, cand = item
        return (len(key[1]) > 1, -self._score(cand), key)

    @staticmethod
    def _candidate_sort_key(cand: Candidate) -> Tuple[bool, float, IndexKey]:
        return (len(cand.key[1]) > 1, -cand.score, cand.key)

    @staticmethod
    def _is_width2(key: IndexKey) -> bool:
        return isinstance(key, tuple) and len(key) == 2 and isinstance(key[1], tuple) and (len(key[1]) == 2)

    @staticmethod
    def _fmt_index_key(key: IndexKey) -> str:
        return f"{key[0]}({','.join(key[1])})"

    def _best_eq_cols(self, evidence: QueryEvidence, table: str) -> List[str]:
        return unique_keep_order((evidence.filter_eq.get(table, []) or []) + (evidence.join_eq.get(table, []) or []))

    def _extract_evidence(self, workload_lines: Sequence[str]) -> Tuple[List[QueryEvidence], Counter]:
        evidences: List[QueryEvidence] = []
        parse_status = Counter()
        for qid, line in enumerate(workload_lines):
            evidence = self.extractor.extract_line(line, qid)
            parse_status[evidence.parse_status] += 1
            evidences.append(evidence)
        return (evidences, parse_status)

    def _emit_single_probes(self, evidence: QueryEvidence) -> Dict[IndexKey, Candidate]:
        out: Dict[IndexKey, Candidate] = {}
        source = 'AST' if evidence.parse_status == 'ast_ok' else 'STATIC_FALLBACK'
        for table, cols in evidence.filter_eq.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family='EQ1', source=source, roles=('filter_eq',), confidence=POLICY_DEFAULTS['candidate_confidence_eq'])
        for table, cols in evidence.join_eq.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family='JOIN_EQ1', source=source, roles=('join_eq',), confidence=POLICY_DEFAULTS['candidate_confidence_join'])
        for table, cols in evidence.filter_rng.items():
            for col in cols:
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family='RANGE1', source=source, roles=('range',), confidence=POLICY_DEFAULTS['candidate_confidence_range'])
        return out

    def _add_vacuum_rescue(self, evidence: QueryEvidence, out: Dict[IndexKey, Candidate]) -> None:
        present_tables = {key[0] for key in out}
        for table in sorted(evidence.tables):
            if table in present_tables:
                continue
            evidence_cols = unique_keep_order((evidence.filter_eq.get(table, []) or []) + (evidence.join_eq.get(table, []) or []) + (evidence.filter_rng.get(table, []) or []))
            for col in evidence_cols:
                key = (table, (col,))
                if self._is_fixed(key):
                    continue
                self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(col,), family='VACUUM_RESCUE1', source='VACUUM_RESCUE', roles=('rescue',), confidence=POLICY_DEFAULTS['candidate_confidence_rescue'])
                break

    def _query_reduce(self, out):
        table_counts = defaultdict(int)
        selected = {}
        for key, cand in sorted(out.items(), key=self._query_sort_key):
            if table_counts[key[0]] >= self.per_table_cap:
                continue
            selected[key] = cand
            table_counts[key[0]] += 1
            if len(selected) >= self.per_query_cap:
                break
        return selected

    @staticmethod
    def _clone_candidate(cand: Candidate) -> Candidate:
        cloned = Candidate(key=cand.key, family=cand.family, source=cand.source, confidence=cand.confidence, roles=tuple(cand.roles))
        cloned.query_ids = set(cand.query_ids)
        cloned.template_ids = set(cand.template_ids)
        cloned.support_count = int(cand.support_count)
        cloned.score = float(cand.score)
        return cloned

    def _merge_candidate(self, merged: Dict[IndexKey, Candidate], key: IndexKey, cand: Candidate, grow_meta: Dict[IndexKey, Dict[str, object]]) -> None:
        existing = merged.get(key)
        if existing is None:
            merged[key] = self._clone_candidate(cand)
            return
        existing.query_ids |= cand.query_ids
        existing.template_ids |= cand.template_ids
        existing.support_count = len(existing.query_ids)
        if self.FAMILY_SCORE.get(cand.family, 0) > self.FAMILY_SCORE.get(existing.family, 0):
            existing.family = cand.family
            existing.source = cand.source
            existing.roles = cand.roles
            existing.confidence = max(existing.confidence, cand.confidence)
        if key in grow_meta:
            grow_meta[key]['support_query_ids'] = sorted(existing.query_ids)

    @classmethod
    def _diagnostic_pair_type_for_rank(cls, cand: Candidate, grow_meta: Optional[Dict[IndexKey, Dict[str, object]]]=None) -> str:
        meta = (grow_meta or {}).get(cand.key, {}) or {}
        expected = str(meta.get('expected_structural_pair_type', '') or '')
        if expected:
            return expected
        family = str(cand.family or '')
        if family in {'JOIN_RANGE', 'EQ_RANGE', 'JOIN_EQ', 'EQ_EQ'}:
            return family
        grow_reason = str(meta.get('grow_reason', '') or '')
        seed_families = {str(x) for x in meta.get('grow_seed_family_set', []) if str(x)}
        seed_family = str(meta.get('grow_seed_family', '') or '')
        if seed_family:
            seed_families.add(seed_family)
        join_seeded = any((fam.startswith('JOIN') for fam in seed_families))
        if 'range' in grow_reason:
            return 'JOIN_RANGE' if join_seeded else 'EQ_RANGE'
        if 'eq' in grow_reason:
            return 'JOIN_EQ' if join_seeded else 'EQ_EQ'
        return family

    @classmethod
    def _fairness_pair_priority(cls, cand: Candidate, grow_meta: Optional[Dict[IndexKey, Dict[str, object]]]=None) -> int:
        pair_type = cls._diagnostic_pair_type_for_rank(cand, grow_meta)
        if pair_type in {'JOIN_RANGE', 'EQ_RANGE'}:
            return 3
        if pair_type in {'JOIN_EQ', 'EQ_EQ'}:
            return 2
        return 1

    @classmethod
    def _fairness_rank_key(cls, cand: Candidate, grow_meta: Optional[Dict[IndexKey, Dict[str, object]]]=None) -> Tuple[int, float, IndexKey]:
        return (-cls._fairness_pair_priority(cand, grow_meta), -float(cand.score), cand.key)

    def _apply_round_width2_fairness(self, *, selected: List[Candidate], ranked: List[Candidate], limit: int, table_counts: Dict[str, int], per_table_width2_reserve: int, round_width2_reserve: int, grow_meta: Dict[IndexKey, Dict[str, object]]) -> Dict[str, Any]:
        per_table_width2_reserve = max(0, int(per_table_width2_reserve))
        round_width2_reserve = max(0, int(round_width2_reserve))
        if per_table_width2_reserve <= 0 or round_width2_reserve <= 0:
            return {'added': set(), 'displaced': [], 'dedup_count': 0, 'block_reasons': Counter()}
        selected_keys = {cand.key for cand in selected}
        selected_unordered: Dict[str, Set[frozenset]] = defaultdict(set)
        selected_width2_counts = Counter()
        for cand in selected:
            if self._is_width2(cand.key):
                selected_width2_counts[cand.key[0]] += 1
                selected_unordered[cand.key[0]].add(frozenset(cand.key[1]))
        width2_by_table: Dict[str, List[Candidate]] = defaultdict(list)
        for cand in ranked:
            if self._is_width2(cand.key) and cand.key not in selected_keys:
                width2_by_table[cand.key[0]].append(cand)
        for table in list(width2_by_table):
            width2_by_table[table].sort(key=lambda cand: self._fairness_rank_key(cand, grow_meta))
        added: Set[IndexKey] = set()
        displaced: List[IndexKey] = []
        dedup_count = 0
        block_reasons = Counter()
        for table in sorted(width2_by_table):
            if len(added) >= round_width2_reserve:
                break
            if selected_width2_counts[table] >= per_table_width2_reserve:
                continue
            for cand in width2_by_table[table]:
                if len(added) >= round_width2_reserve or selected_width2_counts[table] >= per_table_width2_reserve:
                    break
                if cand.key in selected_keys:
                    continue
                unordered = frozenset(cand.key[1])
                if unordered in selected_unordered[table]:
                    dedup_count += 1
                    continue
                must_displace = len(selected) >= limit or int(table_counts.get(table, 0)) >= self.round_table_cap
                remove_idx = None
                if must_displace:
                    same_table_width1 = [(idx, existing) for idx, existing in enumerate(selected) if existing.key[0] == table and len(existing.key[1]) == 1]
                    if not same_table_width1:
                        block_reasons['no_same_table_width1'] += 1
                        continue
                    remove_idx, _remove_cand = sorted(same_table_width1, key=lambda item: (float(item[1].score), item[1].key))[0]
                if remove_idx is None and (len(selected) + 1 > limit or int(table_counts.get(table, 0)) + 1 > self.round_table_cap):
                    block_reasons['capacity_exceeded'] += 1
                    continue
                if remove_idx is not None:
                    removed = selected.pop(remove_idx)
                    selected_keys.discard(removed.key)
                    displaced.append(removed.key)
                else:
                    table_counts[table] = int(table_counts.get(table, 0)) + 1
                selected.append(cand)
                selected_keys.add(cand.key)
                selected_unordered[table].add(unordered)
                selected_width2_counts[table] += 1
                added.add(cand.key)
        return {'added': added, 'displaced': displaced, 'dedup_count': int(dedup_count), 'block_reasons': block_reasons}

    def _make_seed_states(self, *, seed_benefit: Optional[Dict[IndexKey, float]]=None, seed_seen_count: Optional[Dict[IndexKey, int]]=None, seed_positive_count: Optional[Dict[IndexKey, int]]=None, seed_last_obs_src: Optional[Dict[IndexKey, str]]=None, seed_first_seen_round: Optional[Dict[IndexKey, int]]=None, seed_last_seen_round: Optional[Dict[IndexKey, int]]=None, seed_seen_rounds: Optional[Dict[IndexKey, Set[int]]]=None, seed_normalized_benefit: Optional[Dict[IndexKey, float]]=None) -> Dict[IndexKey, SeedState]:
        keys = set(seed_benefit or {}) | set(seed_seen_count or {}) | set(seed_positive_count or {})
        out: Dict[IndexKey, SeedState] = {}
        for key in keys:
            benefit = float((seed_benefit or {}).get(key, 0.0) or 0.0)
            seen = int((seed_seen_count or {}).get(key, 0) or 0)
            positive = int((seed_positive_count or {}).get(key, 0) or 0)
            last_src = str((seed_last_obs_src or {}).get(key, '') or '')
            mature = seen > 0 and positive > 0 and (benefit > 0.0) and (last_src not in {'NO_HIT', 'ALL_FALLBACK'})
            out[key] = SeedState(key=key, first_seen_round=int((seed_first_seen_round or {}).get(key, 0) or 0), last_seen_round=int((seed_last_seen_round or {}).get(key, 0) or 0), seen_rounds=set((seed_seen_rounds or {}).get(key, set()) or set()), evaluated_count=seen, positive_count=positive, benefit=benefit, normalized_benefit=float((seed_normalized_benefit or {}).get(key, 0.0) or 0.0), last_obs_src=last_src, mature=mature)
        return out

    def _grow_width2(self, evidence: QueryEvidence, singles: Dict[IndexKey, Candidate], seed_states: Dict[IndexKey, SeedState], rejected: Counter, grow_meta: Dict[IndexKey, Dict[str, object]]) -> Dict[IndexKey, Candidate]:
        out: Dict[IndexKey, Candidate] = {}
        source = 'AST' if evidence.parse_status == 'ast_ok' else 'STATIC_FALLBACK'
        if evidence.parse_status != 'ast_ok':
            rejected['rejected_growth_parse_fallback'] += 1
            return out
        if evidence.has_or:
            rejected['rejected_growth_has_or'] += 1
            return out
        for table in sorted(evidence.tables):
            if table in evidence.alias_ambiguous_tables:
                rejected['rejected_growth_alias_ambiguous'] += 1
                continue
            eq_cols = self._best_eq_cols(evidence, table)
            rng_cols = unique_keep_order(evidence.filter_rng.get(table, []) or [])
            for seed_col in eq_cols:
                seed_key = (table, (seed_col,))
                seed_cand = singles.get(seed_key)
                seed_state = seed_states.get(seed_key)
                if seed_cand is None:
                    continue
                if seed_cand.family == 'RANGE1':
                    rejected['rejected_growth_range_seed'] += 1
                    continue
                if seed_state is None or seed_state.evaluated_count <= 0:
                    rejected['rejected_growth_seed_unseen'] += 1
                    continue
                if not seed_state.mature:
                    rejected['rejected_growth_seed_not_positive'] += 1
                    continue
                for col in eq_cols:
                    if col == seed_col:
                        continue
                    key = (table, (seed_col, col))
                    self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(seed_col, col), family='EQ_EQ', source=source, roles=('seed_eq', 'eq'), confidence=POLICY_DEFAULTS['candidate_confidence_pair_eq'])
                    if key in out:
                        self._record_grow_meta(grow_meta, key, seed_state, 'seed_eq_plus_eq', seed_cand.family)
                for col in rng_cols:
                    if col == seed_col:
                        continue
                    key = (table, (seed_col, col))
                    self._emit(out, query_id=evidence.query_id, template_id=evidence.template_id, table=table, cols=(seed_col, col), family='EQ_RANGE', source=source, roles=('seed_eq', 'range'), confidence=POLICY_DEFAULTS['candidate_confidence_pair_range'])
                    if key in out:
                        self._record_grow_meta(grow_meta, key, seed_state, 'seed_eq_plus_range', seed_cand.family)
        return out

    @staticmethod
    def _seed_meta(seed: SeedState, grow_reason: str, seed_family: str='') -> Dict[str, object]:
        return {'seed_key': seed.key, 'grow_seed_key': seed.key, 'grow_seed_family': seed_family, 'grow_seed_family_set': [seed_family] if seed_family else [], 'seed_benefit': seed.benefit, 'seed_normalized_benefit': seed.normalized_benefit, 'seed_evaluated_count': seed.evaluated_count, 'seed_positive_count': seed.positive_count, 'seed_first_seen_round': seed.first_seen_round, 'seed_last_seen_round': seed.last_seen_round, 'seed_seen_rounds': sorted(seed.seen_rounds), 'seed_last_obs_src': seed.last_obs_src, 'seed_mature': seed.mature, 'grow_reason': grow_reason, 'rejected_growth_reason': ''}

    @classmethod
    def _record_grow_meta(cls, grow_meta: Dict[IndexKey, Dict[str, object]], key: IndexKey, seed: SeedState, grow_reason: str, seed_family: str) -> None:
        meta = grow_meta.get(key)
        if meta is None:
            grow_meta[key] = cls._seed_meta(seed, grow_reason, seed_family)
            return
        families = set((str(x) for x in meta.get('grow_seed_family_set', []) if str(x)))
        if seed_family:
            families.add(str(seed_family))
        meta['grow_seed_family_set'] = sorted(families)
        if not meta.get('grow_seed_family') and seed_family:
            meta['grow_seed_family'] = seed_family
        if not meta.get('grow_seed_key'):
            meta['grow_seed_key'] = seed.key

    def _candidate_meta(self, cand: Candidate) -> Dict[str, object]:
        meta: Dict[str, object] = {'family': cand.family, 'source': cand.source, 'confidence': cand.confidence, 'support_count': cand.support_count, 'score': cand.score, 'roles': list(cand.roles), 'width_before_merge': len(cand.key[1]), 'width_after_merge': len(cand.key[1])}
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
            seed_key = self._as_tuple_index(meta.get('grow_seed_key', meta.get('seed_key', None)))
            seed_meta = meta_map.get(seed_key, {}) if seed_key is not None and isinstance(meta_map, dict) else {}
            seed_family = str(meta.get('grow_seed_family', '') or '')
            if not seed_family and isinstance(seed_meta, dict):
                seed_family = str(seed_meta.get('family', '') or '')
            seed_families = {str(x) for x in meta.get('grow_seed_family_set', []) if str(x)}
            if seed_family:
                seed_families.add(seed_family)
            grow_reason = str(meta.get('grow_reason', '') or '')
            if grow_reason and (not seed_families):
                seed_missing += 1
                meta['seed_family_missing'] = 1
            else:
                meta['seed_family_missing'] = 0
            family = str(meta.get('family', '') or '')
            join_seeded = any((fam.startswith('JOIN') for fam in seed_families))
            expected_join_family = ''
            if join_seeded and family == 'EQ_RANGE':
                expected_join_family = 'JOIN_RANGE'
            elif join_seeded and family == 'EQ_EQ':
                expected_join_family = 'JOIN_EQ'
            if expected_join_family:
                mismatch += 1
                join_downgraded += 1
                meta['pair_family_vs_grow_reason_mismatch'] = 1
                meta['expected_structural_pair_type'] = expected_join_family
            else:
                meta['pair_family_vs_grow_reason_mismatch'] = 0
            meta['join_seed_downgraded'] = int(bool(expected_join_family))
            if grow_reason and seed_families and (family not in {'EQ_RANGE', 'EQ_EQ', 'JOIN_RANGE', 'JOIN_EQ'}):
                meta['pair_family_vs_grow_reason_mismatch'] = 1
                mismatch += 1
        return {'pair_family_vs_grow_reason_mismatch': int(mismatch), 'seed_family_missing_count': int(seed_missing), 'join_seed_downgraded_count': int(join_downgraded)}

    def generate(self, workload_lines, *, old_conf=None, topk=40, workload_count=0, seed_benefit=None, seed_seen_count=None, seed_positive_count=None, seed_last_obs_src=None, seed_first_seen_round=None, seed_last_seen_round=None, seed_seen_rounds=None, seed_normalized_benefit=None):
        per_query, merged, grow_meta = ([], {}, {})
        rejected = Counter()
        evidences, _parse_status = self._extract_evidence(workload_lines)
        seed_states = self._make_seed_states(seed_benefit=seed_benefit, seed_seen_count=seed_seen_count, seed_positive_count=seed_positive_count, seed_last_obs_src=seed_last_obs_src, seed_first_seen_round=seed_first_seen_round, seed_last_seen_round=seed_last_seen_round, seed_seen_rounds=seed_seen_rounds, seed_normalized_benefit=seed_normalized_benefit)
        stage = 'probe' if int(workload_count) < self.probe_rounds else 'grow'
        for evidence in evidences:
            qmap = self._emit_single_probes(evidence)
            if stage == 'grow':
                qmap.update(self._grow_width2(evidence, qmap, seed_states, rejected, grow_meta))
            self._add_vacuum_rescue(evidence, qmap)
            qmap = self._query_reduce(qmap)
            per_query.append(set(qmap))
            for key, cand in qmap.items():
                self._merge_candidate(merged, key, cand, grow_meta)
        for cand in merged.values():
            cand.score = self._score(cand)
        selected = self._round_select(merged, topk, grow_meta)
        meta_map = {}
        for key, cand in merged.items():
            meta_map[key] = self._candidate_meta(cand)
            if len(key[1]) == 2:
                meta_map[key].update(grow_meta.get(key, {'rejected_growth_reason': 'missing_seed_provenance'}))
        self._annotate_pair_fidelity(meta_map)
        self.last_meta = meta_map
        return GenerationResult(per_query, {c.key for c in selected}, {c.key: c.score for c in selected}, meta_map, stage)

    def _round_select(self, merged, topk, grow_meta=None):
        ranked = sorted(merged.values(), key=self._candidate_sort_key)
        table_counts = defaultdict(int)
        selected = []
        limit = max(1, int(topk))
        for cand in ranked:
            if len(selected) >= limit:
                break
            if table_counts[cand.key[0]] >= self.round_table_cap:
                continue
            selected.append(cand)
            table_counts[cand.key[0]] += 1
        self._apply_round_width2_fairness(selected=selected, ranked=ranked, limit=limit, table_counts=table_counts, per_table_width2_reserve=POLICY_DEFAULTS['pair_supply_per_table_width2_reserve'], round_width2_reserve=POLICY_DEFAULTS['pair_supply_round_width2_reserve'], grow_meta=grow_meta or {})
        return selected
