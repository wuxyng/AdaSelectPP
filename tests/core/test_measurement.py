import copy
import pytest
from adasel.measurement import COMPLETE, BUDGET_DEFER, EVALUATION_FAILED, CONTEXT_INVALID
from support import Backend, tuner
A = ("t", ("a",))
PAIR = ("t", ("a", "b"))
def observe(t, idx=PAIR, mask=None):
    b=t.cost_eval
    if mask is None:mask=[{idx}]+[set() for _ in b.workload[1:]]
    return t._test_candidate(idx,mask,b.roots,sum(b.roots),set(b.old),b.workload)
@pytest.mark.parametrize('targets,expected', [([4, 5], 11), ([15, 18], -13), ([10, 10], 0)])
@pytest.mark.parametrize('mask', [[set(), set()], [{PAIR}, set()], [{PAIR}, {PAIR}]])
def test_full_mask_invariance_empty_signed_once(targets, expected, mask):
    b = Backend(['q1', 'q2'], [10, 10], dict(zip(['q1', 'q2'], targets)))
    t = tuner(b)
    assert observe(t, mask=mask) == COMPLETE
    assert t._last_obs_delta_map[PAIR] == expected
    assert t.idx_seen_cnt[PAIR] == 1
    assert t.idx_last_obs_src[PAIR] == 'OK'
    assert t._last_candidate_observation['measured_occurrences'] == 2
    assert b.target == b.old

def test_duplicate_sql_and_distinct_parameters_are_occurrences():
    b = Backend(['select 1', 'select 1', 'select 2'], [10, 10, 20], {'select 1': 4, 'select 2': 23})
    t = tuner(b)
    assert observe(t) == COMPLETE
    assert t._last_obs_delta_map[PAIR] == 9
    assert len(b.calls) == b.qcp_budget.used == 3

@pytest.mark.parametrize('idx,old', [(A, ()), (PAIR, (PAIR,))])
def test_width1_and_keep_retain_mask(idx, old):
    b = Backend(['q1', 'q2'], [10, 10], {'q1': 4, 'q2': 100}, old)
    t = tuner(b)
    assert observe(t, idx) == COMPLETE
    assert len(b.calls) == 1
    assert t._last_obs_delta_map[idx] == (-6 if old else 6)
    assert not t._last_candidate_observation['full_window']

@pytest.mark.parametrize('failure', [RuntimeError('failed EXPLAIN'), float('nan'), float('inf')])
def test_failed_full_preserves_all_history_and_charges_attempt(failure):
    b = Backend(['q1', 'q2'], [10, 10], {'q1': 5, 'q2': failure})
    t = tuner(b)
    t.columns_benefit[PAIR] = 13
    before = copy.deepcopy({k: v for k, v in t.__dict__.items() if k.startswith('idx_') or k == 'columns_benefit'})
    assert observe(t) == EVALUATION_FAILED
    assert all((getattr(t, k) == v for k, v in before.items()))
    assert PAIR not in t._last_obs_delta_map
    assert b.qcp_budget.used == 2 and b.target == b.old

@pytest.mark.parametrize('cap,status', [(1, BUDGET_DEFER), (2, COMPLETE)])
def test_budget_boundary(cap, status):
    b = Backend(['q1', 'q2'], [10, 10], {'q1': 5, 'q2': 5}, cap=cap)
    t = tuner(b)
    assert observe(t) == status
    assert b.qcp_budget.used == (2 if status == COMPLETE else 0)
    assert bool(t.idx_seen_cnt) == (status == COMPLETE)

@pytest.mark.parametrize('kind', ['identity', 'empty', 'length', 'root_nonfinite', 'missing_safe_backend'])
def test_context_invalid_never_updates(kind):
    b = Backend(['q1'], [10], {'q1': 5})
    t = tuner(b)
    if kind == 'identity':
        b.valid = False
    if kind == 'empty':
        b.workload = []
        b.roots = []
    if kind == 'length':
        b.roots = [10, 10]
    if kind == 'root_nonfinite':
        b.roots = [float('nan')]
    if kind == 'missing_safe_backend':
        b.validate_full_add_context = None
    assert observe(t, mask=[set() for _ in b.workload]) == CONTEXT_INVALID
    assert not t.columns_benefit and (not b.calls)

def test_full_add_at_capacity_is_hypothetical_and_retains_root():
    old = {(f't{i}', ('a',)) for i in range(10)}
    b = Backend(['q1'], [10], {'q1': 5}, old)
    t = tuner(b)
    t.max_num = 10
    assert observe(t) == COMPLETE
    assert len(b.calls[0][1]) == 11 and b.target == old

def test_context_changed_after_paid_full_measurement_does_not_update():
    b = Backend(['q1'], [10], {'q1': 5})
    checks = iter([True, False])
    b.validate_full_add_context = lambda *args: next(checks)
    t = tuner(b)
    assert observe(t) == CONTEXT_INVALID
    assert not t.columns_benefit and (not t.idx_seen_cnt)
    assert b.qcp_budget.used == 1 and b.target == b.old
