"""Acceptance regressions from the ebffc711 independent re-audit, plus adjacent gaps."""
import inspect
from pathlib import Path
import sys
import time

import pytest
from audit_execution_support import run_fixture, purposes, PgError, release
from database.core_backend import Meter
from database.safe_backend import CampaignStop
from experiments.core_v2.receipts import ReceiptMeter


@pytest.mark.parametrize('method,boundary',[
    (ReceiptMeter.begin,'self.inflight = token'),
    (Meter.begin,'self.attempts += 1'),
])
def test_signal_between_charge_and_receipt_never_claims_exact_actual(monkeypatch,tmp_path,method,boundary):
    source,start=inspect.getsourcelines(method)
    line=start+next(i for i,text in enumerate(source) if boundary in text)
    fired=[]
    def interrupt(frame,event,arg):
        if event=='line' and frame.f_code is method.__code__ and frame.f_lineno==line:
            fired.append(line)
            raise CampaignStop('INJECTED_SIGNAL_AFTER_CHARGE')
        return interrupt
    previous=sys.gettrace();sys.settrace(interrupt)
    try:r=run_fixture(monkeypatch,tmp_path,[12.25,12.25])
    finally:sys.settrace(previous)
    m=r['manifest']
    assert fired==[line] and r['failure']['type']=='CampaignStop'
    assert not any(e[0]=='measurement' for e in r['events'])
    assert m['qcp_used']==m['qcp_budget_charge']==1 and m['qcp_confirmed_lower_bound']==0
    assert m['qcp_actual_attempts'] is None and not m['qcp_counters_complete']


def test_count_completeness_requires_confirmed_equal_charged(tmp_path):
    meter=ReceiptMeter(time.time()+60,tmp_path/'qcp.json')
    assert meter.counts_complete
    meter.attempts=1
    assert not meter.counts_complete  # No inflight flag is not proof of a response.


@pytest.mark.parametrize('fatal',[True,False])
def test_final_receipt_failure_keeps_primary_and_records_secondary(monkeypatch,tmp_path,fatal):
    original=release.write_json;failed=[]
    def fault(path,value):
        if Path(path).name=='run_manifest.json' and value.get('resources_complete') is True:
            failed.append(True)
            raise OSError('INJECTED_FINAL_RECEIPT_WRITE_FAILURE')
        return original(path,value)
    monkeypatch.setattr(release,'write_json',fault)
    r=run_fixture(monkeypatch,tmp_path,[PgError('08006','PRIMARY_CONNECTION_FAILURE') if fatal else 12.25,12.25])
    assert failed and r['failure']['type']==('CampaignStop' if fatal else 'OSError')
    if fatal:
        assert r['runtime_attempts']==1 and not purposes(r)
        assert 'PRIMARY_CONNECTION_FAILURE' in r['failure']['error']
    assert any(e.get('stage')=='final_checkpoint' and 'INJECTED_FINAL_RECEIPT_WRITE_FAILURE' in e['error'] for e in r['error_events'])
    assert r['manifest']['status']=='RUNNING' and not r['manifest']['resources_complete']
    case=r['report']['workloads'][0]
    assert case['qcp_actual_attempts'] is None and case['qcp_budget_charge']==20
    assert not case['performance_metrics_valid']
    assert [e[1] for e in r['events'] if e[0]=='close']==['runtime','measurement','schema']
