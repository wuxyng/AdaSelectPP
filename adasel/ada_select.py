"""AdaSelectPP Core v2: bounded candidates, observations, QCP scan, conservative selection.

Reference component semantics: 97d9259 M2Q. Dispatch intentionally differs:
no paid diagnostic, no candidate-count allowance, and no first-round exemption.
"""
from __future__ import annotations
import copy
import logging
import math
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from adasel.measurement import QCPBudget, COMPLETE, BUDGET_DEFER, EVALUATION_FAILED, CONTEXT_INVALID, NO_HIT
from adasel.policy import PolicyConfig, RuntimeConfig, POLICY_DEFAULTS
from adaselect_pp.candidate_gen_v2 import MCIGCandidateGenerator
from util.benefit_normalizer import BenefitNormalizer

logger = logging.getLogger(__name__)
IndexKey = Tuple[str, Tuple[str, ...]]
DEFAULT_COST = POLICY_DEFAULTS['creation_cost_default']

def _canon(key):
    if len(key) >= 2 and not isinstance(key[1], tuple):
        return str(key[0]).lower(), tuple(str(c).lower() for c in key[1:])
    return str(key[0]).lower(), tuple(str(c).lower() for c in key[1])

class AdaSelect:
    def __init__(self, benchmark, backend, policy, *, runtime=None, observer=None):
        self.policy = policy if isinstance(policy, PolicyConfig) else PolicyConfig.from_mapping(policy)
        self.runtime = runtime or RuntimeConfig()
        self.max_num, self.alpha_init, self.beta = self.policy.max_num, self.policy.alpha, self.policy.beta
        self.timeout = self.runtime.timeout_ms
        for name, value in POLICY_DEFAULTS.items():
            setattr(self, name, value)
        self.benchmark = benchmark
        self.cost_eval = self.db_con1 = self.db_con2 = backend
        self.observer = observer
        self.window_id = None
        self.qcp_budget = None
        self.workload_count = 0
        self.consecutive_timeouts = 0
        self.last_stable_config = set()
        self.columns_benefit = {}
        for name in ('idx_alphas', 'idx_error_smooth', 'idx_abs_error_smooth',
                     'idx_seen_cnt', 'idx_positive_cnt', 'idx_first_seen_round',
                     'idx_last_seen_round', 'idx_seen_rounds', 'idx_last_err_sign',
                     'idx_sign_smooth', 'idx_last_obs_src', 'idx_last_observation_scope',
                     'idx_last_observation_kind'):
            setattr(self, name, {})
        self.candidate_generator = MCIGCandidateGenerator(
            benchmark=benchmark, db_con=backend, max_width=self.max_width,
            max_num=max(1, self.candidate_topk_factor*self.max_num+self.candidate_topk_min_extra), indexable_path=self.runtime.indexable_columns_path,
            per_query_cap=self.candidate_per_query_cap, per_table_cap=self.candidate_per_table_cap,
            round_table_cap=self.candidate_round_table_cap)
        self.benefit_norm = BenefitNormalizer()
        self.benefit_norm.load_creation_costs(benchmark, required=True, db_con=backend,
                                            vocabulary=self.candidate_generator.vocab)
        self._reset_round()

    def _reset_round(self):
        self._last_obs_delta_map = {}
        self._last_obs_src_map = {}
        self._last_evaluated_set = set()
        self._last_eval_order = []
        self.ordinary_order = []
        self._last_appearing_set = set()
        self._last_candidate_conf = set()
        self._last_final_conf = set()
        self._last_net_benefit_map = {}
        self._last_decision_stats = {}
        self._last_base_total = None
        self._last_candidate_observation = None
        self.observations = []
        self.batch = SimpleNamespace(stage='', metadata={}, scores={}, candidates=set(), query_membership=[])
        self.baseline_status = None
        self.baseline_error = None
        self.structural_priority = None

    def _emit(self, event):
        if self.observer is not None:
            try:
                self.observer(copy.deepcopy(event))
            except Exception:
                logger.exception('observer failed; policy state is unchanged')

    def bind_window(self, window_id, qcp_budget):
        from adasel.policy import nonnegative_integer
        nonnegative_integer(window_id, 'window_id')
        nonnegative_integer(qcp_budget, 'qcp_budget')
        if self.window_id == window_id:
            if self.qcp_budget.cap != qcp_budget:
                raise ValueError('cannot change budget inside a physical window')
            return
        if self.window_id is not None and window_id < self.window_id:
            raise ValueError('physical window_id must advance')
        self.window_id = window_id
        self.qcp_budget = QCPBudget(qcp_budget)
        self.cost_eval.bind_budget(self.qcp_budget)

    def _candidate_meta_map(self):
        return self.batch.metadata

    def _initial_costs(self, workload):
        costs = [float(self.cost_eval.calculate_now_cost([q])) for q in workload]
        if not all(math.isfinite(c) and c >= 0 for c in costs):
            raise ValueError('invalid baseline observation')
        total = float(sum(costs))
        if not math.isfinite(total):
            raise ValueError('invalid baseline total')
        return costs, total

    def _generate_and_merge_candidates(self, workload, old_conf=None):
        result = self.candidate_generator.generate(
            workload, old_conf=set(old_conf or set()),
            topk=max(1, self.candidate_topk_factor*self.max_num+self.candidate_topk_min_extra), workload_count=self.workload_count,
            seed_benefit=self.columns_benefit, seed_seen_count=self.idx_seen_cnt,
            seed_positive_count=self.idx_positive_cnt, seed_last_obs_src=self.idx_last_obs_src,
            seed_first_seen_round=self.idx_first_seen_round, seed_last_seen_round=self.idx_last_seen_round,
            seed_seen_rounds=self.idx_seen_rounds,
            seed_normalized_benefit=self._log_positive_norm(self.columns_benefit))
        appearing = set(result.topk_set)
        for idx in appearing:
            self.columns_benefit.setdefault(idx, 0.0)
            if len(idx[1]) == 1:
                self.idx_first_seen_round.setdefault(idx, self.workload_count)
                self.idx_last_seen_round[idx] = self.workload_count
                self.idx_seen_rounds.setdefault(idx, set()).add(self.workload_count)
        query_indexes = [set(q) for q in result.query_indexes]
        self.batch = SimpleNamespace(stage=result.stage, metadata=result.meta_map,
                                     scores=result.score_map, candidates=appearing,
                                     query_membership=query_indexes)
        return query_indexes, appearing

    def _choose_lambda(self, idx, prev, obs, *, obs_src='', hit_cnt=0, ok_cnt=0, **unused):
        return self._calculate_adaptive_lambda(idx, prev, obs, obs_src=obs_src,
                                               hit_cnt=hit_cnt, ok_cnt=ok_cnt)

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
        if hasattr(self.benefit_norm, 'creation_cost_for'):
            return float(self.benefit_norm.creation_cost_for(key[0], tuple(key[1]), DEFAULT_COST))
        return float(self.benefit_norm.index_costs.get(tuple(key[1]), DEFAULT_COST))

    def _structural_pair_type(self, key: IndexKey, meta_map: Optional[Dict[IndexKey, Dict[str, Any]]]=None) -> str:
        if len(key[1]) != 2:
            return ''
        meta_map = meta_map if isinstance(meta_map, dict) else self._candidate_meta_map()
        meta = meta_map.get(key, {}) if isinstance(meta_map, dict) else {}
        family = str(meta.get('family', '') or '') if isinstance(meta, dict) else ''
        explicit_type = str(meta.get('structural_pair_type', '') or '') if isinstance(meta, dict) else ''
        if explicit_type:
            return explicit_type
        seed_key = meta.get('seed_key', None) if isinstance(meta, dict) else None
        seed_family = ''
        if isinstance(seed_key, tuple) and len(seed_key) == 2 and isinstance(seed_key[1], tuple):
            seed_meta = meta_map.get(seed_key, {}) if isinstance(meta_map, dict) else {}
            if isinstance(seed_meta, dict):
                seed_family = str(seed_meta.get('family', '') or '')
        if family == 'EQ_RANGE' and seed_family == 'JOIN_EQ1':
            return 'JOIN_RANGE'
        if family == 'EQ_EQ' and seed_family == 'JOIN_EQ1':
            return 'JOIN_EQ'
        return family

    def _is_structural_pair_candidate(self, key: IndexKey, old_conf: Set[IndexKey], meta_map: Optional[Dict[IndexKey, Dict[str, Any]]]=None) -> bool:
        if len(key[1]) != 2 or key in old_conf:
            return False
        meta_map = meta_map if isinstance(meta_map, dict) else self._candidate_meta_map()
        meta = meta_map.get(key, {}) if isinstance(meta_map, dict) else {}
        family = str(meta.get('family', '') or '') if isinstance(meta, dict) else ''
        if family and family not in {'EQ_RANGE', 'EQ_EQ', 'JOIN_RANGE', 'JOIN_EQ'}:
            return False
        grow_reason = str(meta.get('grow_reason', '') or '') if isinstance(meta, dict) else ''
        pair_type = self._structural_pair_type(key, meta_map)
        return pair_type in {'JOIN_RANGE', 'EQ_RANGE', 'JOIN_EQ', 'EQ_EQ'} or grow_reason in {'seed_eq_plus_range', 'seed_eq_plus_eq', 'JOIN_RANGE', 'JOIN_EQ'} or family in {'EQ_RANGE', 'EQ_EQ'}

    def _rank_structural_pair_candidates(self, candidates: Sequence[IndexKey], meta_map: Optional[Dict[IndexKey, Dict[str, Any]]]=None) -> List[IndexKey]:
        meta_map = meta_map if isinstance(meta_map, dict) else self._candidate_meta_map()
        priority = POLICY_DEFAULTS['structural_priority']

        def _as_float(value: Any, default: float=0.0) -> float:
            try:
                return float(value)
            except Exception:
                return default

        def _sort_key(key: IndexKey) -> Tuple[int, float, float, float, IndexKey]:
            meta = meta_map.get(key, {}) if isinstance(meta_map, dict) else {}
            pair_type = self._structural_pair_type(key, meta_map)
            seed_norm = _as_float(meta.get('seed_normalized_benefit', 0.0) if isinstance(meta, dict) else 0.0)
            score = _as_float(self.batch.scores.get(key, meta.get('score', 0.0) if isinstance(meta, dict) else 0.0))
            return (priority.get(pair_type, 99), -seed_norm, -score, self._creation_cost(key), key)
        return sorted(candidates, key=_sort_key)

    def _calculate_adaptive_lambda(self, idx: IndexKey, prediction: float, observation: float, *, obs_src: str='', hit_cnt: int=0, ok_cnt: int=0) -> float:
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
            self.idx_sign_smooth[idx] = POLICY_DEFAULTS['sign_initial']
            return float(self.alpha_init)
        if obs_src in ('NO_HIT', 'ALL_FALLBACK') or hit_cnt <= 0 or ok_cnt <= 0:
            prev_lam = float(self.idx_alphas.get(idx, float(self.alpha_init)))
            regress = max(0.0, min(1.0, float(getattr(self, 'ts_gate_regress', 0.05))))
            lam = (1.0 - regress) * prev_lam + regress * float(self.alpha_init)
            lam = max(float(self.lambda_min), min(float(self.lambda_max), lam))
            self.idx_alphas[idx] = lam
            return lam
        q_weight = max(0.0, min(1.0, float(ok_cnt) / float(max(1, hit_cnt))))
        error = (float(observation) - float(prediction)) * q_weight
        old_rsfe = float(self.idx_error_smooth.get(idx, 0.0))
        old_mad = float(self.idx_abs_error_smooth.get(idx, 0.0))
        rho = max(0.0, min(POLICY_DEFAULTS['tracking_decay_ceiling'], float(getattr(self, 'rsfe_decay', 0.9))))
        beta = max(0.0, min(1.0, float(getattr(self, 'beta_error', 0.2))))
        new_rsfe = rho * old_rsfe + (1.0 - rho) * error
        new_mad = (1.0 - beta) * old_mad + beta * abs(error)
        scale = max(1.0, abs(float(prediction)), abs(float(observation)))
        mad_floor = max(0.0, float(getattr(self, 'ts_mad_floor_rel', 1e-06))) * scale
        new_mad = max(new_mad, mad_floor)
        self.idx_error_smooth[idx] = new_rsfe
        self.idx_abs_error_smooth[idx] = new_mad
        prev_sign = int(self.idx_last_err_sign.get(idx, 0))
        sign = 1 if error > 0 else -1 if error < 0 else 0
        prev_smooth = float(self.idx_sign_smooth.get(idx, POLICY_DEFAULTS['sign_initial']))
        sign_decay = max(0.0, min(POLICY_DEFAULTS['tracking_decay_ceiling'], float(getattr(self, 'ts_sign_decay', 0.9))))
        if sign != 0 and prev_sign != 0:
            same = 1.0 if sign == prev_sign else 0.0
            smooth = sign_decay * prev_smooth + (1.0 - sign_decay) * same
        else:
            smooth = sign_decay * prev_smooth + (1.0 - sign_decay) * POLICY_DEFAULTS['sign_initial']
        self.idx_sign_smooth[idx] = smooth
        if sign != 0:
            self.idx_last_err_sign[idx] = sign
        ts = abs(new_rsfe) / (new_mad + POLICY_DEFAULTS['tracking_epsilon'])
        base_low = float(getattr(self, 'ts_low', 0.5))
        base_high = float(getattr(self, 'ts_high', 2.0))
        if smooth >= POLICY_DEFAULTS['sign_high']:
            mult = POLICY_DEFAULTS['sign_high_multiplier']
        elif smooth <= POLICY_DEFAULTS['sign_low']:
            mult = POLICY_DEFAULTS['sign_low_multiplier']
        else:
            mult = 1.0
        ts_low = max(POLICY_DEFAULTS['tracking_threshold_min'], base_low * mult)
        ts_high = max(ts_low + POLICY_DEFAULTS['tracking_threshold_min'], base_high * mult)
        if ts <= ts_low:
            raw_lambda = float(self.lambda_max)
        elif ts >= ts_high:
            raw_lambda = float(self.lambda_min)
        else:
            ratio = (ts - ts_low) / (ts_high - ts_low)
            raw_lambda = float(self.lambda_max) - ratio * (float(self.lambda_max) - float(self.lambda_min))
        lam = max(float(self.lambda_min), min(float(self.lambda_max), raw_lambda))
        self.idx_alphas[idx] = lam
        return lam

    def _test_candidate(self, idx_key: IndexKey, query_indexes: List[Set[IndexKey]], base_costs: List[float], base_total: float, old_conf: Set[IndexKey], workload: List[str]) -> str:
        repair = bool(len(idx_key[1]) == 2 and idx_key not in old_conf)
        mask_hits = sum((idx_key in qs for qs in query_indexes))
        required = len(workload) if repair else mask_hits
        outcome = {'index': idx_key, 'full_window': repair, 'query_mask_hits': mask_hits, 'measured_occurrences': 0, 'required_qcp': required, 'status': None, 'kind': 'KEEP' if idx_key in old_conf else 'ADD', 'scope': 'FULL_WINDOW_ADD' if repair else 'LEGACY_MASK_APPROX'}
        self._last_candidate_observation = outcome
        if repair:
            valid_shape = bool(workload) and len(workload) == len(base_costs) == len(query_indexes) and all((math.isfinite(float(c)) for c in base_costs)) and math.isfinite(float(base_total)) and math.isclose(sum(base_costs), base_total, abs_tol=POLICY_DEFAULTS['full_identity_abs_tol'], rel_tol=POLICY_DEFAULTS['full_identity_rel_tol'])
            validate = getattr(self.cost_eval, 'validate_full_add_context', None)
            if not valid_shape or validate is None or (not validate(workload, base_costs, old_conf, idx_key)):
                outcome['status'] = CONTEXT_INVALID
                return CONTEXT_INVALID
        budget = getattr(self.cost_eval, 'qcp_budget', None)
        if not isinstance(budget, QCPBudget):
            raise ValueError('a physical-window QCP budget is required')
        if not budget.fits(required):
            outcome['status'] = BUDGET_DEFER
            return BUDGET_DEFER
        tbl, cols = idx_key
        if idx_key in old_conf:
            self.db_con2.disable_index(tbl, cols)
        else:
            self.db_con1.create_index(tbl, cols)
        total_cost = 0.0
        hit_cnt = ok_cnt = fail_cnt = 0
        try:
            for i, (q_idxs, base_cost) in enumerate(zip(query_indexes, base_costs)):
                if repair or idx_key in q_idxs:
                    hit_cnt += 1
                    try:
                        value = float(self.cost_eval.calculate_now_cost([workload[i]]))
                        if repair and (not math.isfinite(value) or value < 0):
                            raise ValueError('invalid full-window target cost')
                        total_cost += value
                        ok_cnt += 1
                    except Exception as e:
                        fail_cnt += 1
                        if repair:
                            outcome.update(status=EVALUATION_FAILED, measured_occurrences=ok_cnt, failure=str(e))
                            return EVALUATION_FAILED
                        total_cost += float(base_cost)
                else:
                    total_cost += float(base_cost)
        finally:
            if idx_key in old_conf:
                self.db_con2.enable_index(tbl, cols)
            else:
                self.db_con1.drop_index(tbl, cols)
        if repair and (not math.isfinite(total_cost)):
            outcome.update(status=EVALUATION_FAILED, measured_occurrences=ok_cnt)
            return EVALUATION_FAILED
        if repair and (not validate(workload, base_costs, old_conf, idx_key)):
            outcome.update(status=CONTEXT_INVALID, measured_occurrences=ok_cnt)
            return CONTEXT_INVALID
        delta = float(base_total - total_cost) if idx_key not in old_conf else float(total_cost - base_total)
        prev = float(self.columns_benefit.get(idx_key, 0.0))
        if hit_cnt <= 0:
            obs_src = 'NO_HIT'
        elif ok_cnt <= 0:
            obs_src = 'ALL_FALLBACK'
        elif ok_cnt < hit_cnt:
            obs_src = 'PARTIAL_FALLBACK'
        else:
            obs_src = 'OK'
        lam = self._choose_lambda(idx_key, prev, delta, obs_src=obs_src, hit_cnt=hit_cnt, ok_cnt=ok_cnt, fail_cnt=fail_cnt)
        new_benefit = lam * prev + (1.0 - lam) * delta
        self.columns_benefit[idx_key] = new_benefit
        self.idx_alphas[idx_key] = lam
        self.idx_seen_cnt[idx_key] = int(self.idx_seen_cnt.get(idx_key, 0)) + 1
        if len(idx_key[1]) == 1 and delta > 0.0 and (obs_src not in ('NO_HIT', 'ALL_FALLBACK')):
            self.idx_positive_cnt[idx_key] = int(self.idx_positive_cnt.get(idx_key, 0)) + 1
        self._last_obs_delta_map[idx_key] = delta
        self._last_obs_src_map[idx_key] = obs_src
        self.idx_last_obs_src[idx_key] = obs_src
        status = NO_HIT if obs_src == 'NO_HIT' else COMPLETE
        outcome.update(status=status, measured_occurrences=ok_cnt, raw_delta=delta,
                       lambda_used=lam, mu=new_benefit, observation_source=obs_src)
        self.idx_last_observation_scope[idx_key] = outcome['scope']
        self.idx_last_observation_kind[idx_key] = outcome['kind']
        return status

    def _ordered_candidates(self, appearing, old_conf):
        normalized = self._log_positive_norm({idx: self.columns_benefit.get(idx, 0.0) for idx in appearing})
        ordinary = [idx for idx, _ in sorted(normalized.items(), key=lambda kv: (-kv[1], kv[0]))]
        self.ordinary_order = list(ordinary)
        structural = []
        if self.batch.stage == 'grow':
            structural = self._rank_structural_pair_candidates(
                [idx for idx in appearing if self._is_structural_pair_candidate(idx, old_conf)])
        self.structural_priority = structural[0] if structural else None
        if structural:
            return [structural[0]] + [idx for idx in ordinary if idx != structural[0]]
        return ordinary

    def _estimate_benefits(self, workload, old_conf):
        self._reset_round()
        if not workload:
            self.baseline_status = CONTEXT_INVALID
            return False
        if not self.qcp_budget.fits(len(workload)):
            self.baseline_status = BUDGET_DEFER
            return False
        try:
            self.cost_eval.begin_root(workload, old_conf)
            base_costs, base_total = self._initial_costs(workload)
            self.cost_eval.record_root(workload, base_costs, old_conf)
        except Exception as exc:
            self.baseline_status = EVALUATION_FAILED
            self.baseline_error = str(exc)
            self._emit(dict(event='baseline_failed',error=str(exc)))
            return False
        self.baseline_status = COMPLETE
        self._last_base_total = base_total
        query_indexes, appearing = self._generate_and_merge_candidates(workload, old_conf=old_conf)
        self._last_appearing_set = set(appearing)
        if not appearing:
            return True
        self._last_eval_order = self._ordered_candidates(appearing, old_conf)
        assert len(self._last_eval_order) == len(set(self._last_eval_order)) == len(appearing)
        for idx in self._last_eval_order:
            before = self.qcp_budget.used
            status = self._test_candidate(idx, query_indexes, base_costs, base_total, old_conf, workload)
            observation = dict(self._last_candidate_observation, qcp_used=self.qcp_budget.used-before)
            self.observations.append(observation)
            self._emit(dict(event='observation', **observation))
            if status == COMPLETE:
                self._last_evaluated_set.add(idx)
            if status == CONTEXT_INVALID:
                raise RuntimeError('measurement context identity changed')
        remaining = self.qcp_budget.cap - self.qcp_budget.used
        assert all(o['required_qcp'] > remaining for o in self.observations if o['status'] == BUDGET_DEFER)
        for key in list(self.columns_benefit):
            if key in appearing:
                continue
            decay = max(0.0, min(1.0, float(self.idx_alphas.get(key, self.alpha_init))))
            self.columns_benefit[key] *= decay
            if key in self.idx_error_smooth:
                self.idx_error_smooth[key] *= self.rsfe_decay
            if key in self.idx_abs_error_smooth:
                self.idx_abs_error_smooth[key] *= self.rsfe_decay
        return True

    def _choose_config(self, old_conf: Set[IndexKey]) -> Set[IndexKey]:
        old_canon = {_canon(k) for k in old_conf}
        normalized = self._log_positive_norm(self.columns_benefit)
        net: Dict[IndexKey, float] = {}
        for key, val in normalized.items():
            cost = 0.0 if key in old_canon else self._creation_cost(key)
            net[key] = float(val) - float(cost)
        self._last_net_benefit_map = dict(net)
        sorted_keys = sorted(net.items(), key=lambda x: x[1], reverse=True)
        ranked = sorted_keys[:self.max_num]
        filtered_nonpositive_count = sum((1 for key, value in ranked if key not in old_canon and float(value) <= 0.0))
        candidate_conf = {key for key, value in ranked if key in old_canon or float(value) > 0.0}
        self._last_candidate_conf = set(candidate_conf)
        if self.workload_count == 0:
            selected_conf = set(candidate_conf)
            ratio = float('inf') if candidate_conf else 0.0
            old_benefit = 0.0
            new_benefit = sum((net.get(k, 0.0) for k in selected_conf))
        else:
            old_benefit = sum((net.get(k, 0.0) for k in old_canon))
            new_benefit = sum((net.get(k, 0.0) for k in candidate_conf))
            eps = POLICY_DEFAULTS['selector_epsilon']
            ratio = float('-inf')
            if old_benefit > eps and new_benefit > eps:
                ratio = new_benefit / old_benefit
            elif old_benefit < -eps and new_benefit < -eps and (abs(new_benefit) > eps):
                ratio = abs(old_benefit) / abs(new_benefit)
            elif old_benefit < -eps and new_benefit > eps:
                ratio = float('inf')
            selected_conf = set(candidate_conf) if ratio > self.beta else set(old_canon)
        self._last_final_conf = set(selected_conf)
        self._last_decision_stats = {'old_benefit': float(old_benefit), 'new_benefit': float(new_benefit), 'ratio': float(ratio), 'beta': float(self.beta), 'filtered_nonpositive_count': float(filtered_nonpositive_count)}
        return set(selected_conf)

    def _handle_timeout_reset(self, old_conf: Set[IndexKey]) -> None:
        try:
            self.db_con2.drop_all_indexes()
        except Exception as exc:
            pass
        old_conf.clear()
        self.columns_benefit.clear()
        self.idx_alphas.clear()
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
        self.idx_last_observation_scope.clear()
        self.idx_last_observation_kind.clear()
        self.workload_count = 0
        self.consecutive_timeouts = 0

    def run(self, workload, old_conf, runtimes=None, *, window_id, qcp_budget=None):
        if getattr(self, '_processed_window', None) == window_id:
            raise ValueError('a physical window is processed once; no automatic retry')
        self.bind_window(window_id, self.policy.qcp_budget if qcp_budget is None else qcp_budget)
        self._processed_window = window_id
        if runtimes and any(float(rt) >= self.timeout for rt in runtimes):
            self._handle_timeout_reset(old_conf)
        valid = self._estimate_benefits(workload, old_conf)
        selected = self._choose_config(old_conf) if valid else set(old_conf)
        if not valid:
            self._last_candidate_conf = set(old_conf)
        self._last_final_conf = set(selected)
        self.workload_count += 1
        return selected

Tuner = AdaSelect
