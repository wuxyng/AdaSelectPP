import copy
from types import SimpleNamespace
import pytest

from adasel.measurement import COMPLETE, BUDGET_DEFER, EVALUATION_FAILED, NO_HIT, QCPBudget
from adasel.policy import PolicyConfig
from support import Backend, tuner, supply, history


def keys(n):return [('t', (str(i),)) for i in range(n)]


def test_q1_beyond_old_two_candidate_boundary_actual_six_measurements():
    candidates = keys(8)
    backend = Backend(['q0','q1'], [10,10], cap=14)
    t = supply(tuner(backend), candidates, [set(candidates)]*2)
    t.run(backend.workload, set(), window_id=0)
    assert t._last_eval_order == candidates
    assert t._last_evaluated_set == set(candidates[:6])
    assert [o['status'] for o in t.observations] == [COMPLETE]*6+[BUDGET_DEFER]*2
    assert len(backend.calls) == t.qcp_budget.used == 14


def test_q2_large_full_task_deferred_later_small_tasks_fit():
    pair=('t',('a','b')); a,b=keys(2)
    w=[f'q{i}' for i in range(21)]
    backend=Backend(w,[10]*21,cap=29)
    masks=[({a} if i<3 else set()) | ({b} if i<4 else set()) for i in range(21)]
    t=supply(tuner(backend),[pair,a,b],masks,stage='grow',metadata={pair:{'family':'EQ_RANGE'}})
    before=copy.deepcopy(t.columns_benefit)
    t.run(w,set(),window_id=0)
    assert t._last_eval_order==[pair,a,b]
    assert [o['required_qcp'] for o in t.observations]==[21,3,4]
    assert [o['status'] for o in t.observations]==[BUDGET_DEFER,COMPLETE,COMPLETE]
    assert t.qcp_budget.used==28 and pair not in t.idx_seen_cnt


def test_q3_first_round_is_not_free_full_evaluation():
    candidates=[('t',(str(i),'b')) for i in range(3)]
    backend=Backend([f'q{i}' for i in range(5)],[10]*5,cap=10)
    t=supply(tuner(backend),candidates,[set(candidates)]*5)
    t.workload_count=0
    t.run(backend.workload,set(),window_id=0)
    assert len(t._last_evaluated_set)==1 and t.qcp_budget.used==10
    assert len(t._last_eval_order)==3


@pytest.mark.parametrize('budget',[0,4])
def test_q4_incomplete_baseline_holds_without_requests_or_history(budget):
    old={('t',('old',))}
    backend=Backend([f'q{i}' for i in range(5)],[10]*5,old=old,cap=budget)
    t=tuner(backend);before=history(t)
    assert t.run(backend.workload,set(old),window_id=0)==old
    assert not backend.calls and not t.observations
    assert history(t)==before and t.baseline_status==BUDGET_DEFER


def test_q4_failed_baseline_charged_holds_and_never_generates():
    backend=Backend(['q1','q2'],[10,RuntimeError('root failure')],cap=4)
    t=tuner(backend)
    assert t.run(backend.workload,set(),window_id=0)==set()
    assert t.baseline_status==EVALUATION_FAILED and len(backend.calls)==2
    assert not t.observations and not t.idx_seen_cnt


def test_q5_structural_priority_has_no_old_m_ge_two_condition():
    pair=('t',('z','x')); ordinary=keys(3)
    backend=Backend(['q1','q2'],[10,10],cap=4)
    t=supply(tuner(backend),ordinary+[pair],[set(ordinary)]*2,stage='grow',metadata={pair:{'family':'EQ_RANGE'}})
    t.columns_benefit={k:10 for k in ordinary}
    t.run(backend.workload,set(),window_id=0)
    assert t._last_eval_order==[pair]+ordinary
    assert t._last_evaluated_set=={pair}
    assert all(len(target)<=1 for _,target in backend.calls)


def test_q6_same_physical_window_reset_never_regrants_budget():
    backend=Backend(['q'],[10],cap=10);t=tuner(backend)
    t.bind_window(8,10)
    for _ in range(7):t.qcp_budget.charge()
    t._handle_timeout_reset(set())
    t.bind_window(8,10)
    assert t.qcp_budget.used==7 and t.qcp_budget.cap-t.qcp_budget.used==3
    with pytest.raises(ValueError):t.bind_window(8,11)
    t.bind_window(9,2)
    assert t.qcp_budget.used==0 and t.qcp_budget.cap==2


def test_q7_partial_full_failure_paid_cleanup_history_unchanged_no_retry():
    pair=('t',('a','b')); w=['q1','q2','q3']
    backend=Backend(w,[10]*3,{pair:{'q1':5,'q2':RuntimeError('fail'),'q3':5}},cap=9)
    t=supply(tuner(backend),[pair],[{pair}]*3)
    t.columns_benefit[pair]=7
    before=history(t)
    t.run(w,set(),window_id=0)
    assert history(t)==before and len(t.observations)==1
    assert t.observations[0]['status']==EVALUATION_FAILED
    assert t.qcp_budget.used==5 and backend.target==set() and backend.cleanups==1


def test_q8_zero_request_no_hit_once_even_at_zero_balance():
    a,b=keys(2);backend=Backend(['q1','q2'],[10,10],cap=4)
    t=supply(tuner(backend),[a,b],[{a}]*2)
    t.run(backend.workload,set(),window_id=0)
    assert [o['status'] for o in t.observations]==[COMPLETE,NO_HIT]
    assert t.qcp_budget.used==4 and len(t.observations)==2
    assert t.observations[1]['scope']=='LEGACY_MASK_APPROX'
    assert b not in t._last_evaluated_set


def test_q8_balance_is_allowed_when_all_eligible_tasks_processed():
    candidate=keys(1);backend=Backend(['q'],[10],cap=10)
    t=supply(tuner(backend),candidate,[set(candidate)])
    t.run(backend.workload,set(),window_id=0)
    assert t.qcp_budget.used==2 and len(t.observations)==1


@pytest.mark.parametrize('bad',[None,True,False,-1,1.0,'10'])
def test_q10_budget_is_explicit_nonnegative_integer(bad):
    with pytest.raises(ValueError):QCPBudget(bad)
    with pytest.raises(ValueError):PolicyConfig(10,bad,.5,1.1)


@pytest.mark.parametrize('key',['optimizer_ratio','ratio','max_evaluated_candidates','job_cap','width2_add_full_window','candidate_generation_mode','lambda_policy','unknown'])
def test_q10_unknown_or_retired_policy_input_rejected(key):
    with pytest.raises(ValueError):PolicyConfig.from_mapping(dict(max_num=10,qcp_budget=20,alpha=.5,beta=1.1,**{key:1}))


def test_q11_one_guard_counts_root_add_keep_full_and_failure():
    old={('t',('old',))};pair=('t',('a','b'));single=('t',('s',))
    backend=Backend(['q1','q2'],[10,10],old=old,cap=8)
    t=supply(tuner(backend),[pair,single,*old],[{single,*old}]*2)
    t.run(backend.workload,set(old),window_id=0)
    assert t.qcp_budget is backend.qcp_budget and t.qcp_budget.used==8
    assert {o['kind'] for o in t.observations}=={'ADD','KEEP'}
    assert all(o['qcp_used']==2 for o in t.observations)


def test_fixed_order_not_resorted_after_current_observation():
    candidates=keys(3);backend=Backend(['q'],[10],cap=4)
    t=supply(tuner(backend),candidates,[set(candidates)])
    t.columns_benefit={candidates[0]:1,candidates[1]:3,candidates[2]:2}
    t.run(backend.workload,set(),window_id=0)
    assert t._last_eval_order==[candidates[1],candidates[2],candidates[0]]


def test_observer_is_copy_only_no_feedback_or_extra_calls():
    def run(observer):
        backend=Backend(['q'],[10],cap=5)
        t=supply(tuner(backend),keys(3),[set(keys(3))]);t.observer=observer
        selected=t.run(backend.workload,set(),window_id=0)
        return selected,history(t),backend.calls,t.observations
    def malicious(event):event.clear()
    assert run(None)==run(malicious)


@pytest.mark.parametrize('benchmark',['job','tpch','tpchs','unlisted'])
def test_no_hidden_benchmark_count_cap(benchmark):
    candidates=keys(50);backend=Backend(['q'],[10],cap=51)
    t=supply(tuner(backend),candidates,[set(candidates)]);t.benchmark=benchmark
    t.run(backend.workload,set(),window_id=0)
    assert len(t._last_evaluated_set)==50 and t.qcp_budget.used==51


def test_probe_order_and_metadata_only_pair_are_not_promoted():
    a,b=keys(2);pair=('t',('z','x'));backend=Backend(['q'],[10],cap=5)
    t=supply(tuner(backend),[a,b],[{a,b}],stage='grow',metadata={pair:{'family':'EQ_RANGE'}})
    t.run(backend.workload,set(),window_id=0)
    assert t._last_eval_order==[a,b] and t.structural_priority is None
    backend=Backend(['q'],[10],cap=2)
    t=supply(tuner(backend),[a,pair],[{a,pair}],stage='probe',metadata={pair:{'family':'EQ_RANGE'}})
    t.run(backend.workload,set(),window_id=0)
    assert t._last_eval_order==[a,pair] and t.structural_priority is None
    assert t._last_evaluated_set=={a}


def test_same_window_cannot_be_processed_again_or_recharge():
    backend=Backend(['q'],[10],cap=2);t=supply(tuner(backend),keys(1),[set(keys(1))])
    t.run(backend.workload,set(),window_id=3)
    with pytest.raises(ValueError,match='processed once'):
        t.run(backend.workload,set(),window_id=3)
    assert t.qcp_budget.used==2 and len(backend.calls)==2


def test_absent_history_decay_matches_original_formula():
    a,b=keys(2);backend=Backend(['q'],[10],cap=2)
    t=supply(tuner(backend),[a],[{a}]);t.columns_benefit[b]=8
    t.idx_alphas[b]=.3;t.idx_error_smooth[b]=2;t.idx_abs_error_smooth[b]=4
    t.run(backend.workload,set(),window_id=0)
    assert t.columns_benefit[b]==8*.3
    assert t.idx_error_smooth[b]==2*.9 and t.idx_abs_error_smooth[b]==4*.9
