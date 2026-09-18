import csv
import json
from experiments.core_v2.report import summarize


def test_offline_boundary_report_uses_actual_paid_observations_and_checks_accounting(tmp_path):
    name='tpch_shifting';d=tmp_path/'runs'/name;d.mkdir(parents=True)
    candidates=[['t',[str(i)]] for i in range(8)]
    observations=[dict(index=k,status='COMPLETE' if i<6 else 'BUDGET_DEFER',scope='LEGACY_MASK_APPROX',required=2,paid=2 if i<6 else 0) for i,k in enumerate(candidates)]
    row=dict(window=0,qcp_cap=14,qcp_used=14,qcp_remaining=0,root_qcp=2,
             ordinary_order=json.dumps(candidates),structural_priority='null',internal_round_after=3,
             outcomes=json.dumps(observations),deferred=2,baseline_status='COMPLETE')
    with (d/'rounds.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=row);writer.writeheader();writer.writerow(row)
    m=dict(status='COMPLETE',rounds=1,qcp_used=14,revision='fixture',exact_restoration=True,stranded_budget=0)
    (d/'run_manifest.json').write_text(json.dumps(m))
    registration=dict(profiles={'cases':[dict(name=name,rounds=1,round_size=2,qcp_budget_per_window=[14])]},automatic_followup='STOP')
    (tmp_path/'run_manifest.json').write_text(json.dumps(registration))
    result=summarize(tmp_path);case=result['workloads'][0]
    # Archived TPCH .5 boundary is four; six actually completed, two of them beyond it.
    assert case['beyond_historical_nomination_complete']==2
    assert case['beyond_historical_nomination_qcp']==4
    assert result['status']=='CORE_V2_BLOCKED'  # One fixture is never a nine-workload release.
    row['qcp_used']=13
    with (d/'rounds.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=row);writer.writeheader();writer.writerow(row)
    import pytest
    with pytest.raises(AssertionError):summarize(tmp_path)


def test_interrupted_round_qcp_is_counted_and_bad_execution_is_invalid(tmp_path):
    name='tpch_shifting';d=tmp_path/'runs'/name;d.mkdir(parents=True)
    row=dict(window=0,qcp_cap=14,qcp_used=2,qcp_remaining=12,root_qcp=2,
             ordinary_order='[]',structural_priority='null',internal_round_after=1,
             outcomes='[]',deferred=0,baseline_status='COMPLETE')
    with (d/'rounds.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=row);writer.writeheader();writer.writerow(row)
    m=dict(status='INCOMPLETE',rounds=1,qcp_used=5,revision='fixture',exact_restoration=True,stranded_budget=0)
    (d/'run_manifest.json').write_text(json.dumps(m))
    (d/'error_events.jsonl').write_text(json.dumps(dict(window=0,occurrence=0,sqlstate=None,error='TypeError'))+'\n')
    registration=dict(profiles={'cases':[dict(name=name,rounds=2,round_size=2,qcp_budget_per_window=[14,14])]},automatic_followup='STOP')
    (tmp_path/'run_manifest.json').write_text(json.dumps(registration))
    result=summarize(tmp_path);case=result['workloads'][0]
    assert case['unfinished_round_qcp']==3 and case['execution_protocol_errors']==1
    assert not case['performance_metrics_valid'] and result['status']=='CORE_V2_BLOCKED'
