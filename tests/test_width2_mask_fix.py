import copy
import importlib.util
import math
from pathlib import Path
import subprocess
from types import MethodType, SimpleNamespace

import pytest

from adasel.ada_select import AdaSelect
from adasel.measurement import QCPBudget, COMPLETE, BUDGET_DEFER, EVALUATION_FAILED, CONTEXT_INVALID

A = ("t", ("a",))
PAIR = ("t", ("a", "b"))


class Backend:
    def __init__(self, workload, roots, targets, old=(), cap=100):
        self.workload = list(workload)
        self.roots = list(roots)
        self.targets = targets
        self.old = set(old)
        self.target = set(old)
        self.calls = []
        self.qcp_budget = QCPBudget(cap)
        self.valid = True

    def create_index(self, table, cols):
        self.target.add((table, tuple(cols)))

    def drop_index(self, table, cols):
        self.target.discard((table, tuple(cols)))

    disable_index = drop_index
    enable_index = create_index

    def validate_full_add_context(self, workload, costs, old, idx):
        return self.valid and list(workload) == self.workload and list(costs) == self.roots and set(old) == self.old

    def calculate_now_cost(self, workload):
        assert len(workload) == 1
        self.qcp_budget.charge()
        self.calls.append((workload[0], frozenset(self.target)))
        value = self.targets[workload[0]]
        if isinstance(value, Exception):
            raise value
        return value


def tuner(backend, enabled=True, state=None, cls=AdaSelect):
    t = cls.__new__(cls)
    t.width2_add_full_window = enabled
    t.columns_benefit = {}
    t.alpha_init = .65
    t.lambda_policy = "adaptive"
    t.lambda_min, t.lambda_max = .2, .95
    t.beta_error = .2
    t.rsfe_decay = .9
    t.workload_count = 2
    for name in ("idx_alphas", "idx_alphas_shadow", "idx_error_smooth", "idx_abs_error_smooth",
                 "idx_seen_cnt", "idx_positive_cnt", "idx_last_err_sign", "idx_sign_smooth",
                 "idx_last_obs_src", "_last_obs_delta_map", "_last_obs_src_map"):
        setattr(t, name, {})
    t._m_stats = {"what_if_calls": 0}
    if state:
        t.__dict__.update(copy.deepcopy(state))
    t.cost_eval = t.db_con1 = t.db_con2 = backend
    return t


def observe(t, idx=PAIR, mask=None):
    b = t.cost_eval
    if mask is None:
        mask = [{idx}] + [set() for _ in b.workload[1:]]
    return t._test_candidate(idx, mask, b.roots, sum(b.roots), set(b.old), b.workload)


@pytest.mark.parametrize("targets,expected", [([4, 5], 11), ([15, 18], -13), ([10, 10], 0)])
@pytest.mark.parametrize("mask", [[set(), set()], [{PAIR}, set()], [{PAIR}, {PAIR}]])
def test_full_mask_invariance_empty_signed_once(targets, expected, mask):
    b = Backend(["q1", "q2"], [10, 10], dict(zip(["q1", "q2"], targets)))
    t = tuner(b)
    assert observe(t, mask=mask) == COMPLETE
    assert t._last_obs_delta_map[PAIR] == expected
    assert t.idx_seen_cnt[PAIR] == 1
    assert t.idx_last_obs_src[PAIR] == "OK"
    assert t._last_candidate_observation["measured_occurrences"] == 2
    assert b.target == b.old


def test_duplicate_sql_and_distinct_parameters_are_occurrences():
    b = Backend(["select 1", "select 1", "select 2"], [10, 10, 20], {"select 1": 4, "select 2": 23})
    t = tuner(b)
    assert observe(t) == COMPLETE
    assert t._last_obs_delta_map[PAIR] == 9
    assert len(b.calls) == b.qcp_budget.used == 3


@pytest.mark.parametrize("idx,old", [(A, ()), (PAIR, (PAIR,))])
def test_width1_and_keep_retain_mask(idx, old):
    b = Backend(["q1", "q2"], [10, 10], {"q1": 4, "q2": 100}, old)
    t = tuner(b)
    assert observe(t, idx) == COMPLETE
    assert len(b.calls) == 1
    assert t._last_obs_delta_map[idx] == (-6 if old else 6)
    assert not t._last_candidate_observation["full_window"]


@pytest.mark.parametrize("failure", [RuntimeError("failed EXPLAIN"), float("nan"), float("inf")])
def test_failed_full_preserves_all_history_and_charges_attempt(failure):
    b = Backend(["q1", "q2"], [10, 10], {"q1": 5, "q2": failure})
    t = tuner(b)
    t.columns_benefit[PAIR] = 13
    before = copy.deepcopy({k: v for k, v in t.__dict__.items() if k.startswith("idx_") or k == "columns_benefit"})
    assert observe(t) == EVALUATION_FAILED
    assert all(getattr(t, k) == v for k, v in before.items())
    assert PAIR not in t._last_obs_delta_map
    assert b.qcp_budget.used == 2 and b.target == b.old


@pytest.mark.parametrize("cap,status", [(1, BUDGET_DEFER), (2, COMPLETE)])
def test_budget_boundary(cap, status):
    b = Backend(["q1", "q2"], [10, 10], {"q1": 5, "q2": 5}, cap=cap)
    t = tuner(b)
    assert observe(t) == status
    assert b.qcp_budget.used == (2 if status == COMPLETE else 0)
    assert bool(t.idx_seen_cnt) == (status == COMPLETE)


@pytest.mark.parametrize("kind", ["identity", "empty", "length", "root_nonfinite", "missing_safe_backend"])
def test_context_invalid_never_updates(kind):
    b = Backend(["q1"], [10], {"q1": 5})
    t = tuner(b)
    if kind == "identity": b.valid = False
    if kind == "empty": b.workload = []; b.roots = []
    if kind == "length": b.roots = [10, 10]
    if kind == "root_nonfinite": b.roots = [float("nan")]
    if kind == "missing_safe_backend": b.validate_full_add_context = None
    assert observe(t, mask=[set() for _ in b.workload]) == CONTEXT_INVALID
    assert not t.columns_benefit and not b.calls


def test_full_add_at_capacity_is_hypothetical_and_retains_root():
    old = {(f"t{i}", ("a",)) for i in range(10)}
    b = Backend(["q1"], [10], {"q1": 5}, old)
    t = tuner(b); t.max_num = 10
    assert observe(t) == COMPLETE
    assert len(b.calls[0][1]) == 11 and b.target == old


def test_context_changed_after_paid_full_measurement_does_not_update():
    b = Backend(["q1"], [10], {"q1": 5})
    checks = iter([True, False])
    b.validate_full_add_context = lambda *args: next(checks)
    t = tuner(b)
    assert observe(t) == CONTEXT_INVALID
    assert not t.columns_benefit and not t.idx_seen_cnt
    assert b.qcp_budget.used == 1 and b.target == b.old


def test_disabled_fairness_lane_and_deferred_lane_not_counted():
    from test_phase05_pr17a_fairness_eval_replacement_lane import _tuner, PAIR_AB
    t = _tuner(enabled=True, quota=1, postround={PAIR_AB})
    t._test_candidate = lambda *args: BUDGET_DEFER
    t._record_structural_pair_replacement_diagnostic = lambda *args: None
    assert t._run_fairness_eval_lane([], [], 0., set(), []) == 0
    assert not t._last_evaluated_set
    assert t._last_wdcg_stats['fairness_eval_lane_evaluated_count'] == 0


def test_original_nomination_order_skip_nonfit_continue_and_diagnostic_charged():
    from test_phase05_pr7b_structural_pair_eval_replacement import _make_tuner, A, B, C, PAIR
    t = _make_tuner()
    b = Backend(["q1", "q2"], [10, 10], {"q1": 5, "q2": 5}, cap=1)
    t.cost_eval = t.db_con1 = t.db_con2 = b
    t.width2_add_full_window = True
    for k,v in tuner(b).__dict__.items():
        if k.startswith('idx_') or k.startswith('_last_obs'): setattr(t,k,v)
    t._test_candidate = MethodType(AdaSelect._test_candidate, t)
    t._initial_costs = lambda w: ([10,10],20)
    original_generate = t._generate_and_merge_candidates
    def generate(w, old_conf=None):
        _, appearing = original_generate(w,old_conf)
        return [{A,PAIR},{B,C}], appearing
    t._generate_and_merge_candidates = generate
    t._estimate_benefits(b.workload,set())
    assert t._last_evaluated_set == {A}
    assert t._m_stats['evaluated_count'] == 1
    assert b.qcp_budget.used == 1
    # The real diagnostic guard must not fabricate a zero or issue an unfitted request.
    t._record_structural_pair_replacement_diagnostic = MethodType(AdaSelect._record_structural_pair_replacement_diagnostic,t)
    t._record_structural_pair_replacement_diagnostic(PAIR,[{PAIR},{PAIR}],[10,10],20,{A},b.workload)
    assert t._last_structural_pair_replacement_map[PAIR]['replacement_obs_src'] == BUDGET_DEFER
    assert t._last_structural_pair_replacement_map[PAIR]['replacement_benefit_raw'] == ''
    assert b.qcp_budget.used == 1


def test_selector_and_candidate_generation_source_unchanged():
    import ast
    root = Path(__file__).resolve().parents[1]
    original = subprocess.check_output(['git','show','1a1129ad3b2dc512a3951aca9cdada97d966e774:adasel/ada_select.py'],cwd=root).decode()
    current = (root/'adasel/ada_select.py').read_text(encoding='utf-8')
    def methods(s):
        cls=next(n for n in ast.parse(s).body if isinstance(n,ast.ClassDef))
        return {n.name:ast.dump(n,include_attributes=False) for n in cls.body if isinstance(n,ast.FunctionDef)}
    a,b=methods(original),methods(current)
    permitted={'__init__','_load_cfg','_test_candidate','_estimate_benefits','_run_fairness_eval_lane','_record_structural_pair_replacement_diagnostic'}
    assert {k for k in a if a[k]!=b[k]} <= permitted
    assert a['_choose_config']==b['_choose_config']
    for file in ('adaselect_pp/candidate_gen_v2/generator.py','util/benefit_normalizer.py'):
        old=subprocess.check_output(['git','show','1a1129ad3b2dc512a3951aca9cdada97d966e774:'+file],cwd=root)
        assert old.replace(b'\r\n',b'\n') == (root/file).read_bytes().replace(b'\r\n',b'\n')
