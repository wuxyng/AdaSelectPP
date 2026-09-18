"""Offline release accounting only; never imported by the online controller."""
import argparse
import csv
import json
from pathlib import Path
from database.execution_errors import classify_error
from experiments.core_v2.receipts import qcp_receipt

ROOT=Path(__file__).resolve().parents[2]
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def key(k):return k[0],tuple(k[1])


def summarize(out):
    registration=read(out/'run_manifest.json')
    historical=read(ROOT/'experiments/reproduce/reference/HISTORICAL_RATIO_METADATA.json')['historical_optimizer_ratio_by_case']
    result=[]
    launched={j['job'][1] for j in registration.get('started_jobs',[]) if j['job'][0]=='run'}
    for case in registration['profiles']['cases']:
        d=out/'runs'/case['name'];mp=d/'run_manifest.json'
        if not mp.exists() and case['name'] not in launched:
            result.append(dict(workload=case['name'],status='NOT_STARTED',rounds=0,expected_rounds=case['rounds']))
            continue
        m=read(mp) if mp.exists() else dict(status='MISSING_WORKER_RECEIPT',rounds=0)
        rows=list(csv.DictReader((d/'rounds.csv').open(encoding='utf-8'))) if (d/'rounds.csv').exists() else []
        checkpoint=read(d/'qcp_receipt.json') if (d/'qcp_receipt.json').exists() else {}
        m.update(qcp_receipt(m,sum(case['qcp_budget_per_window']),checkpoint))
        m['resources_complete']=m.get('resources_complete',m['status'] in ('COMPLETE','INCOMPLETE'))
        m['timing_status']='FINALIZED' if m['resources_complete'] else 'INCOMPLETE_KNOWN_LOWER_BOUNDS_ONLY'
        if not m['resources_complete']:
            m['known_timing_lower_bounds']={k:m.get(k) for k in ('exec_observed_s','exec_protocol_penalty_s','exec_wall_s','rec_s','trans_s','explain_s')}
            if checkpoint:
                m['known_timing_lower_bounds']['explain_s']=checkpoint.get('explain_s')
            m['total_s']=None
        m.update(workload=case['name'],expected_rounds=case['rounds'],rounds=len(rows))
        parent=next((j for j in registration.get('started_jobs',[]) if j['job']==['run',case['name']]),{})
        m['parent_recovery_s']=parent.get('parent_recovery_s')
        m['parent_recovery']=parent.get('recovery')
        m['beyond_historical_nomination_complete']=0;m['beyond_historical_nomination_qcp']=0
        m['beyond_historical_nomination_full']=0;m['full_qcp']=0;m['full_requested']=0
        m['budget_limited_rounds']=0;m['baseline_hold_rounds']=0
        for r in rows:
            w=int(r['window']);assert int(r['qcp_cap'])==case['qcp_budget_per_window'][w]
            assert 0<=int(r['qcp_used'])<=int(r['qcp_cap'])
            ordinary=[key(k) for k in json.loads(r['ordinary_order'])]
            priority=json.loads(r['structural_priority']);priority=key(priority) if priority else None
            # Only classifies this observed Core state against the historical count gate.
            # It does not manufacture a historical trajectory or optimizer response.
            old_count=len(ordinary) if int(r['internal_round_after'])==1 else max(1,int(historical[case['name']]*len(ordinary)))
            prefix=[priority] if priority is not None and old_count>=2 else []
            old_set=set(prefix+[k for k in ordinary if k not in prefix][:max(0,old_count-len(prefix))])
            observations=json.loads(r['outcomes']);paid=sum(o['paid'] for o in observations)
            assert len(observations)==len(ordinary)==len(set(ordinary))
            assert paid+int(r['root_qcp'])==int(r['qcp_used'])
            assert all(o['required']>int(r['qcp_remaining']) for o in observations if o['status']=='BUDGET_DEFER')
            for o in observations:
                if o['scope']=='FULL_WINDOW_ADD':
                    m['full_requested']+=1;m['full_qcp']+=o['paid']
                    if o['status']=='COMPLETE':assert o['paid']==case['round_size'] or w==case['rounds']-1
                if o['status']=='COMPLETE' and key(o['index']) not in old_set:
                    m['beyond_historical_nomination_complete']+=1
                    m['beyond_historical_nomination_qcp']+=o['paid']
                    m['beyond_historical_nomination_full']+=o['scope']=='FULL_WINDOW_ADD'
            m['budget_limited_rounds']+=int(r['deferred'])>0
            m['baseline_hold_rounds']+=r['baseline_status']!='COMPLETE'
        recorded_qcp=sum(int(r['qcp_used']) for r in rows)
        m['unfinished_round_qcp']=m['qcp_used']-recorded_qcp if m['qcp_used'] is not None else None
        assert m['unfinished_round_qcp'] is None or m['unfinished_round_qcp']>=0
        if m['status']=='COMPLETE':assert m['unfinished_round_qcp']==0
        errors=[json.loads(s) for s in (d/'error_events.jsonl').read_text().splitlines()] if (d/'error_events.jsonl').exists() else []
        classified=[classify_error(e) for e in errors if 'occurrence' in e]
        m['execution_protocol_errors']=sum(e['fatal'] for e in classified)
        m['execution_timeout_penalties']=sum(not e['fatal'] for e in classified)
        m['fatal_failures']=sum(classify_error(e)['fatal'] for e in errors if e.get('event')!='measurement_failed')
        m['performance_metrics_valid']=(m['status']=='COMPLETE' and not m['fatal_failures']
                                        and m['qcp_counters_complete'] and m['resources_complete'])
        if m['status']=='COMPLETE':assert len(rows)==case['rounds'] and m['exact_restoration']
        events=[json.loads(s) for s in (d/'ddl_events.jsonl').read_text().splitlines()] if (d/'ddl_events.jsonl').exists() else []
        counts={'create':0,'drop':0};times={'create':0.,'drop':0.};pending=None
        for event in events:
            if event['stage']=='before':
                pending='create' if event['statement'].startswith('CREATE') else 'drop'
                counts[pending]+=1
            else:
                assert pending is not None
                times[pending]+=event['elapsed_ms']/1000;pending=None
        m['ddl_attempt_counts']=counts;m['ddl_attempt_seconds']=times
        m['ddl_timing_complete']=pending is None
        if pending is not None:
            m['ddl_unfinished_kind']=pending
            m['resources_complete']=False;m['performance_metrics_valid']=False
        result.append(m)
    complete=[m for m in result if m['status']=='COMPLETE']
    started=[m for m in result if m['status']!='NOT_STARTED']
    gates={
        'registered_preflight':registration.get('preflight',{}).get('status')=='PASS',
        'nine_workloads_603_windows':len(complete)==9 and sum(m['rounds'] for m in complete)==603,
        'qcp_and_no_stranded_work':bool(started) and all(m.get('qcp_counters_complete') and m.get('stranded_budget')==0 for m in started) and sum(m['qcp_budget_charge'] for m in started)<=22271,
        'restoration_for_started_runs':bool(started) and all(m.get('exact_restoration') for m in started),
        'single_frozen_version_and_stop':bool(started) and all(m.get('revision') for m in started) and len({m.get('revision') for m in started})==1 and registration['automatic_followup']=='STOP',
        'runtime_protocol':len(complete)==9 and all(m.get('performance_metrics_valid') for m in complete),
    }
    report=dict(status='CORE_V2_READY' if all(gates.values()) else 'CORE_V2_BLOCKED',gates=gates,
                registration=registration,workloads=result,
                interpretation='One-version screening only; no significance or historical speedup conclusion.',
                boundary_interpretation='Beyond-count counts use each observed Core state and archived ratio only in this offline report. They are not a historical closed-loop counterfactual. D02 paid-diagnostic calls are zero by source and common-counter accounting; no numerical D02/D07 causal split is inferred.')
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    report=summarize(a.output)
    (a.output/'release_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# AdaSelectPP Core v2 release report','',report['status'],'',
           'One frozen version, one repeat. Screening only; statistical significance is unproven.','',
           '| Workload | Rounds | Exec s | Inclusive rec s | Trans s | Total s | Wall s | QCP / cap | Changes | Full ADD | Deferred |',
           '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for m in report['workloads']:
        if m['status']!='COMPLETE':
            qcp=(f"{m['qcp_actual_attempts']}/{m.get('qcp_cap','unknown')}" if m.get('qcp_actual_attempts') is not None
                 else f"actual unknown; charge {m.get('qcp_budget_charge','unknown')}/{m.get('qcp_cap','unknown')}" if m['status']!='NOT_STARTED' else '—')
            state='INVALID_EXECUTION' if m.get('execution_protocol_errors') else m['status']
            lines.append(f"| {m['workload']} | {m['rounds']}/{m['expected_rounds']} ({state}) | — | — | — | — | — | {qcp} | — | — | — |")
            continue
        values=[m['workload'],f"{m['rounds']}/{m['expected_rounds']}"]+[f"{m[k]:.3f}" for k in ['exec_s','rec_s','trans_s','total_s','wall_s']]+[f"{m['qcp_used']}/{m['qcp_cap']}"]+[str(m[k]) for k in ['changed','width2_full','deferred']]
        lines.append('| '+' | '.join(values)+' |')
    lines+=['','## Gates','']+[f"- {k}: {'PASS' if v else 'FAIL'}" for k,v in report['gates'].items()]
    lines+=['','## Interpretation','',
            'Incomplete or execution-invalid rows are not valid release coverage or performance measurements. Paid QCP, including the unfinished round, remains charged; original artifacts are retained.',
            '',report['boundary_interpretation'],'',
            'Optimizer EXPLAIN, scope-exclusive validation and other recommendation time, DDL build/drop attempts, resets/timeouts, full/approximate coverage, and each workload’s budget boundary counts are recorded in release_report.json. Inclusive recommendation includes setup, validation, safety and logging.','']
    (a.output/'RELEASE_REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    print(report['status'])

if __name__=='__main__':main()
