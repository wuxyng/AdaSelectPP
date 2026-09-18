"""Acceptance inversions of the independent audit's real-entry reproductions."""
import json
import time
from types import SimpleNamespace
import pytest
from audit_execution_support import (run_fixture, campaign_fixture, names, purposes,
                                     PgError, Session, Connection, EPOCH, release)


@pytest.mark.parametrize('code,message',[
    ('08006','connection failure'),('57P01','admin shutdown'),('53100','disk full'),
    ('XX000','internal error'),('42601','syntax error'),('42703','undefined column'),
    ('57014','canceling statement due to user request')])
def test_fatal_stops_before_later_occurrence_tuner_and_reset(monkeypatch,tmp_path,code,message):
    r=run_fixture(monkeypatch,tmp_path,[PgError(code,message),12.25])
    assert r['failure'] and r['runtime_attempts']==1
    assert 'tuner_run' not in names(r) and not purposes(r)
    assert r['manifest']['exec_s']==0 and r['manifest']['exec_protocol_penalty_s']==0
    assert r['error_events'][0]['classification']=='FATAL'
    assert r['report']['workloads'][0]['execution_protocol_errors']==1
    assert not r['report']['workloads'][0]['performance_metrics_valid']


def test_setup_failure_never_becomes_execution_penalty(monkeypatch,tmp_path):
    r=run_fixture(monkeypatch,tmp_path,[12.25,12.25],setup_error=PgError('42501','permission denied'))
    assert r['failure'] and r['runtime_attempts']==0 and not purposes(r)
    assert r['manifest']['exec_s']==0 and r['error_events'][0]['stage']=='setup'


def test_last_fatal_occurrence_never_reaches_tuner(monkeypatch,tmp_path):
    r=run_fixture(monkeypatch,tmp_path,[PgError('08006','dead connection')],round_size=1)
    assert r['failure'] and not purposes(r) and 'tuner_run' not in names(r)


def test_dead_connection_is_not_retried(monkeypatch,tmp_path):
    r=run_fixture(monkeypatch,tmp_path,[PgError('08006','dead'),ConnectionError('still dead')])
    assert r['runtime_attempts']==1 and r['failure']


def test_partial_window_keeps_known_cost_and_execution_wall(monkeypatch,tmp_path):
    r=run_fixture(monkeypatch,tmp_path,[12.25,ConnectionError('died')])
    m=r['manifest']
    assert m['exec_s']==.01225 and m['exec_wall_s']==1.
    assert m['rec_s']==pytest.approx(0.) and m['trans_s']==pytest.approx(.1)
    assert m['runtime_actual_requests'] is None and m['runtime_request_intents']==2
    assert m['runtime_confirmed_requests']==1 and not m['runtime_request_count_complete']
    assert r['report']['workloads'][0]['execution_protocol_errors']==1


def test_timeout_penalty_is_separate_from_actual_measurement(monkeypatch,tmp_path):
    r=run_fixture(monkeypatch,tmp_path,[PgError('57014','canceling statement due to statement timeout'),12.25])
    m=r['manifest']
    assert not r['failure'] and m['exec_s']==30.01225
    assert m.get('exec_observed_s',m['exec_s'])==.01225 and m['exec_protocol_penalty_s']==30.
    assert m['exec_wall_s']==1. and 'NATIVE_TIMEOUT_RESET' in purposes(r)
    assert r['report']['workloads'][0]['execution_timeout_penalties']==1
    assert r['report']['workloads'][0]['performance_metrics_valid']


@pytest.mark.parametrize('option',[dict(cleanup_error=True),dict(close_error='runtime'),dict(final_validation_error=True)])
def test_finalization_preserves_primary_paid_cost_and_all_closes(monkeypatch,tmp_path,option):
    r=run_fixture(monkeypatch,tmp_path,[12.25,12.25],measurement_fail_at=2,**option)
    m=r['manifest']
    assert r['failure']['type']=='CampaignStop' and 'INJECTED_UNRECOVERABLE' in r['failure']['error']
    assert m['status']=='INCOMPLETE' and m['qcp_used']==2 and m['qcp_failures']==1
    assert m['exec_s']==.0245 and m['exec_wall_s']==1.
    assert m['secondary_errors'] and 'report_error' not in r
    assert [e[1] for e in r['events'] if e[0]=='close']==['runtime','measurement','schema']
    # A local interruption cannot prove that both charged intents hit the server.
    assert m['qcp_actual_attempts'] is None and not m['qcp_counters_complete']
    assert m['qcp_budget_charge']==2


def test_transition_failure_cost_is_not_recommendation(monkeypatch,tmp_path):
    r=run_fixture(monkeypatch,tmp_path,[12.25,12.25],transition_error=True)
    assert r['failure'] and r['manifest']['trans_s']==pytest.approx(.4)
    assert r['manifest']['rec_s']==pytest.approx(0.)


@pytest.mark.parametrize('failure',[PgError('08006','connection lost'),ConnectionError('socket closed')])
def test_fatal_optimizer_error_propagates_without_selector_or_reset(monkeypatch,tmp_path,failure):
    r=run_fixture(monkeypatch,tmp_path,[12.25,12.25],measurement_fail_at=1,measurement_error=failure)
    assert r['failure'] and r['failure']['type']=='CampaignStop'
    assert not purposes(r) and r['manifest']['qcp_used']==1
    assert r['manifest']['status']=='INCOMPLETE'


def test_connection_setup_failure_closes_acquired_resources(monkeypatch,tmp_path):
    r=run_fixture(monkeypatch,tmp_path,[12.25,12.25],connect_error='measurement')
    assert r['failure'] and r['manifest'].get('status')=='INCOMPLETE'
    assert [e[1] for e in r['events'] if e[0]=='close']==['runtime']
    assert 'report_error' not in r


def test_parent_interrupt_terminates_waits_then_recovers(monkeypatch,tmp_path):
    r=campaign_fixture(monkeypatch,tmp_path,'parent_interrupt')
    events=r['events'];stops=[i for i,e in enumerate(events) if e[0] in ('terminate','killpg')]
    assert stops and stops[0]<next(i for i,e in enumerate(events) if e[0]=='recover')
    assert any(e[0]=='wait' for e in events[stops[0]+1:])
    assert r['failure']['type']=='KeyboardInterrupt' and len([e for e in events if e[0]=='spawn'])==2
    assert not r['manifest']['qcp_counters_complete'] and r['manifest']['qcp_used'] is None
    assert r['manifest']['qcp_budget_charge']==20


def test_hard_kill_unknown_request_count_is_not_zero(monkeypatch,tmp_path):
    r=campaign_fixture(monkeypatch,tmp_path,'hard_kill')
    assert ['recover'] in r['events']
    assert r['manifest']['qcp_used'] is None and r['manifest']['qcp_actual_attempts'] is None
    assert r['manifest']['qcp_budget_charge']==20 and not r['manifest']['qcp_counters_complete']


def test_recovery_error_retains_original_worker_failure(monkeypatch,tmp_path):
    r=campaign_fixture(monkeypatch,tmp_path,'recovery_failure')
    assert r['failure']['type']=='RuntimeError' and 'WORKER_FAILED' in r['failure']['error']
    assert r['manifest']['qcp_used']==7 and r['manifest']['supervisor_errors'][0]['stage']=='parent_recovery'


def preflight_fixture(monkeypatch,tmp_path,failure=None):
    s=SimpleNamespace(clock=0.,runtime_attempts=0,actions=[failure or 12.25]*3,events=[],setup_error=None,close_error=None)
    class S(Session):
        def _capture_epoch(self,_):return EPOCH.copy()
        def _evaluate(self,query,target):
            token=self.meter.begin();error=None
            try:
                if query.query_id=='exception':raise PgError('42703','injected')
                if query.query_id=='timeout':raise PgError('57014','statement timeout')
                return SimpleNamespace(optimizer_cost=10.)
            except BaseException as exc:error=exc;raise
            finally:self.meter.end(token,time.monotonic(),error)
    m={}
    monkeypatch.setattr(release,'load',lambda _:m)
    monkeypatch.setattr(release,'connect',lambda *a:Connection(s,'preflight'))
    monkeypatch.setattr(release,'schema',lambda _:{'t':['a','b']})
    monkeypatch.setattr(release,'catalog',lambda _:[])
    monkeypatch.setattr(release,'OwnedRoot',lambda *a:SimpleNamespace(cleanup=lambda:None))
    monkeypatch.setattr(release,'make_session',lambda c,relations,meter:S(s,meter,relations))
    monkeypatch.setattr(release,'reference_epoch',lambda *a:EPOCH.copy())
    return s,m


def test_preflight_calls_real_execute_occurrence_and_counts_all_explains(monkeypatch,tmp_path):
    s,m=preflight_fixture(monkeypatch,tmp_path)
    release.preflight(tmp_path,time.time()+60)
    assert s.runtime_attempts==3 and m['preflight']['attempts']==18
    assert m['preflight']['status']=='PASS' and m['preflight']['runtime_checks']==3
    assert len([e for e in s.events if e[0]=='sql' and e[2].startswith('EXPLAIN (ANALYZE, FORMAT JSON)')])==3


def test_preflight_real_runtime_failure_cannot_pass(monkeypatch,tmp_path):
    s,m=preflight_fixture(monkeypatch,tmp_path,TypeError('bad driver'))
    with pytest.raises(BaseException):release.preflight(tmp_path,time.time()+60)
    assert s.runtime_attempts==1 and m['preflight']['status']=='INCOMPLETE'
