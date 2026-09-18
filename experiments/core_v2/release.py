"""One registered Core v2 release campaign: 9 runs, 603 physical windows, then stop."""
import argparse
from collections import defaultdict
import csv
import gzip
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from adasel.ada_select import AdaSelect
from adasel.policy import PolicyConfig,RuntimeConfig,POLICY_DEFAULTS
from adasel.measurement import QCPBudget,COMPLETE,BUDGET_DEFER,EVALUATION_FAILED,NO_HIT
from database.core_backend import Backend,Meter,config
from database.safe_backend import (connect,sqlrows,catalog,qid,OwnedRoot,make_session,
                                   native_tuple,allowed_vocabulary,recover_owned,
                                   write_json,read,append,CampaignStop)
from tools.evaluation_substrate.schema import IndexDefinition,QuerySpec
from tools.evaluation_substrate.reveal import _safe_explain_query
from adaselect_pp.common import sql_only


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(out):
    m=read(out/'run_manifest.json')
    for name,expected in m['release']['files'].items():
        assert sha(ROOT/name)==expected,'FROZEN_SOURCE_CHANGED:'+name
    p=m['profiles']
    assert p['expected_runs']==9 and p['expected_windows']==603 and p['qcp_cap_total']==22271
    assert sum(sum(c['qcp_budget_per_window']) for c in p['cases'])==22271
    for c in p['cases']:
        assert len(c['qcp_budget_per_window'])==c['rounds']
        assert sha(c['workload_locator_hint'])==c['workload_sha256'],'TRACE_IDENTITY_CHANGED'
        assert c['query_timeout_ms']==30000
        for k,v in c['constants_from_source'].items():
            if k in POLICY_DEFAULTS:assert POLICY_DEFAULTS[k]==v,('CONSTANT_CHANGED',k)
    return m


def schema(c):
    result=defaultdict(list)
    for t,col in sqlrows(c,"SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND table_name IN (SELECT tablename FROM pg_tables WHERE schemaname='public') ORDER BY table_name,ordinal_position"):
        result[t].append(col)
    return dict(result)


def same_snapshot(a,b):
    return all(a[k]==b[k] for k in ('database_environment','statistics_fingerprint','schema_fingerprint'))


def execute_occurrence(connection,line,timeout_ms,meter):
    """Original runtime SQL and penalty; programming/validation errors abort the run."""
    statement='EXPLAIN (ANALYZE, FORMAT JSON) '+_safe_explain_query(sql_only(line))
    meter.check()
    timeout=min(timeout_ms,max(1,int((meter.deadline-time.monotonic())*1000)))
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT set_config(%s,%s,false)',('statement_timeout',str(timeout)))
            cursor.execute(statement)
            plan=cursor.fetchone()[0]
    except Exception as exc:
        sqlstate=getattr(exc,'pgcode',None)
        if not isinstance(sqlstate,str) or len(sqlstate)!=5:
            raise CampaignStop('EXECUTION_DRIVER_OR_CONNECTION_FAILURE:'+str(exc)) from exc
        return 30000.,dict(error=str(exc),sqlstate=sqlstate)
    if isinstance(plan,str):plan=json.loads(plan)
    runtime=float(plan[0]['Plan']['Actual Total Time'])
    if not math.isfinite(runtime) or runtime<0:
        raise CampaignStop('INVALID_RUNTIME_OBSERVATION')
    return runtime,None


def reference_epoch(m,db):
    item=m['snapshot_checkpoints'][db]
    assert sha(item['path'])==item['sha256'],'REFERENCE_CHECKPOINT_CHANGED'
    return json.load(gzip.open(item['path'],'rt'))['measurement_epoch']


def preflight(out,deadline):
    m=load(out);meter=Meter(deadline);meter.budget=QCPBudget(30);receipts=[]
    for db in ('tpch','tpchs','job'):
        c=connect(m,db,'preflight');sc=schema(c);before=catalog(c)
        manager=OwnedRoot(c,m,db,out/'preflight'/db,allowed_vocabulary(sc))
        session=make_session(c,[('public',t) for t in sc],meter)
        epoch=session._capture_epoch(session.relevant_relations)
        assert same_snapshot(reference_epoch(m,db),epoch),'SOURCE_SNAPSHOT_IDENTITY_MISMATCH'
        table=next(t for t in sc if len(sc[t])>=2)
        target=config(set(session.physical)|{IndexDefinition(table,tuple(sc[table][:2]))})
        checks=[]
        cases=[('root','SELECT 1',config(session.physical),None,None),
               ('add','SELECT 1',target,None,None),
               ('exception','SELECT core_missing_preflight_column FROM public.'+qid(table),target,None,'42703'),
               ('timeout','SELECT count(*) FROM '+','.join('public.'+qid(table)+' '+qid('a'+str(i)) for i in range(16)),target,'timeout','57014'),
               ('root_recheck','SELECT 1',config(session.physical),None,None)]
        try:
            for name,sql,tgt,fault,expected in cases:
                meter.forced_failure=fault;error=None
                try:session._evaluate(QuerySpec(name,sql),tgt)
                except Exception as exc:error=getattr(exc,'pgcode',str(exc))
                finally:meter.forced_failure=None
                assert error==expected,(db,name,error,expected)
                session._assert_clean();assert catalog(c)==before
                checks.append(dict(test=name,error=error,clean=True))
            assert session._capture_epoch(session.relevant_relations)['epoch_hash']==epoch['epoch_hash']
            receipts.append(dict(database=db,checks=checks,exact_restoration=True,epoch=epoch))
        finally:
            session._assert_clean();assert catalog(c)==before;manager.cleanup();c.close()
    assert meter.attempts<=30
    m['preflight']=dict(status='PASS',attempts=meter.attempts,databases=receipts)
    write_json(out/'run_manifest.json',m)


def run_one(out,name,deadline):
    started=time.monotonic();m=load(out)
    case=next(c for c in m['profiles']['cases'] if c['name']==name);db=case['benchmark']
    d=out/'runs'/name;d.mkdir(parents=True,exist_ok=False)
    c=connect(m,db,'runtime');measure=connect(m,db,'measurement');meta=connect(m,db,'schema')
    tables=schema(c);manager=OwnedRoot(c,m,db,d,allowed_vocabulary(tables))
    meter=Meter(deadline);manager.meter=meter
    backend=Backend(meta,manager);backend.session_meter=meter
    policy=PolicyConfig(**case['policy_inputs'],qcp_budget=case['qcp_budget_per_window'][0])
    tuner=AdaSelect(db,backend,policy,runtime=RuntimeConfig(timeout_ms=case['query_timeout_ms']))
    cost_keys=set(allowed_vocabulary(tables));model=tuner.benefit_norm
    missing=sum(k not in model.index_costs_by_key and
                (k[1] not in model.index_costs or k[1] in model.creation_cost_collisions) for k in cost_keys)
    manifest=dict(revision=m['release']['commit'],workload=name,policy_inputs=case['policy_inputs'],
                  qcp_budget_per_window=case['qcp_budget_per_window'],constants=dict(POLICY_DEFAULTS),
                  workload_sha256=case['workload_sha256'],query_timeout_ms=case['query_timeout_ms'],
                  creation_cost_path=model.creation_cost_path,creation_cost_sha256=sha(ROOT/model.creation_cost_path),
                  creation_cost_missing=missing,creation_cost_allowlist_total=len(cost_keys),
                  creation_cost_missing_fraction=missing/max(1,len(cost_keys)),status='RUNNING',
                  time_definition=m['time_definition'])
    write_json(d/'run_manifest.json',manifest)
    lines=[line.strip() for line in Path(case['workload_locator_hint']).read_text().splitlines() if line.strip()]
    windows=[lines[i:i+case['round_size']] for i in range(0,len(lines),case['round_size'])]
    assert len(windows)==case['rounds'] and len(lines)==case['query_occurrences']
    rows=[];exec_wall=0.;exec_total=0.;trans_wall=0.;status='INCOMPLETE';reference=reference_epoch(m,db)
    def terminate(*_):raise CampaignStop('SUPERVISOR_TERMINATED')
    signal.signal(signal.SIGTERM,terminate)
    stream=(d/'rounds.csv').open('w',encoding='utf-8',newline='');writer=None
    try:
        for i,window in enumerate(windows):
            r0=time.monotonic();meter.check();runtimes=[];errors=[];e0=time.monotonic()
            for j,line in enumerate(window):
                rt,error=execute_occurrence(c,line,case['query_timeout_ms'],meter)
                if error is not None:
                    event=dict(window=i,occurrence=j,**error)
                    errors.append(event);append(d/'error_events.jsonl',event)
                runtimes.append(rt)
            ew=time.monotonic()-e0;execution=sum(runtimes)/1000;exec_wall+=ew;exec_total+=execution
            rec0=time.monotonic();reset_before=manager.ddl_ms
            session=make_session(measure,[('public',t) for t in tables],meter);backend.bind(session)
            epoch=session._capture_epoch(session.relevant_relations)
            assert same_snapshot(reference,epoch),'DATABASE_IDENTITY_CHANGED'
            assert {native_tuple(x) for x in set(session.physical)-session.protected}==manager.current
            old=set(manager.current);original_old=set(old);before_round=tuner.workload_count
            meter.current_window=i;q0=meter.attempts;x0=meter.explain_ms;s0=meter.total_evaluate_ms
            target=tuner.run(window,old,[int(v) for v in runtimes],window_id=i,qcp_budget=case['qcp_budget_per_window'][i])
            assert len(target)<=policy.max_num and set(target)<=set(tuner.columns_benefit)|old
            session._assert_clean();assert same_snapshot(reference,session._capture_epoch(session.relevant_relations))
            reset_s=(manager.ddl_ms-reset_before)/1000;rec=time.monotonic()-rec0-reset_s;trans_wall+=reset_s
            t0=time.monotonic();before_transition=set(manager.current)
            manager.transition(target,'CORE_SELECTOR_RETURNED_CONFIGURATION')
            transition=time.monotonic()-t0+reset_s;trans_wall+=transition-reset_s
            assert meter.attempts-q0==tuner.qcp_budget.used<=tuner.qcp_budget.cap
            observations=tuner.observations;remaining=tuner.qcp_budget.cap-tuner.qcp_budget.used
            if tuner.baseline_status==EVALUATION_FAILED:
                append(d/'error_events.jsonl',dict(window=i,event='baseline_failed',error=tuner.baseline_error))
            assert all(o['required_qcp']>remaining for o in observations if o['status']==BUDGET_DEFER)
            for o in observations:
                if o['status']==EVALUATION_FAILED:append(d/'error_events.jsonl',dict(window=i,event='measurement_failed',observation=o))
            compact=[dict(index=o['index'],status=o['status'],kind=o['kind'],scope=o['scope'],required=o['required_qcp'],paid=o['qcp_used']) for o in observations]
            row=dict(window=i,current=json.dumps(sorted(original_old)),proposed=json.dumps(sorted(tuner._last_candidate_conf)),
                     actual=json.dumps(sorted(manager.current)),
                     current_id=config(set(session.protected)|{IndexDefinition(t,tuple(cs)) for t,cs in original_old}).configuration_id,
                     proposed_id=config(set(session.protected)|{IndexDefinition(t,tuple(cs)) for t,cs in tuner._last_candidate_conf}).configuration_id,
                     measurement_root_id=config(session.physical).configuration_id,
                     actual_id=config(set(session.protected)|{IndexDefinition(t,tuple(cs)) for t,cs in manager.current}).configuration_id,
                     stage=tuner.batch.stage,internal_round_before=before_round,internal_round_after=tuner.workload_count,
                     baseline_status=tuner.baseline_status,nominated=len(tuner._last_eval_order),
                     complete=sum(o['status']==COMPLETE for o in observations),deferred=sum(o['status']==BUDGET_DEFER for o in observations),
                     failed=sum(o['status']==EVALUATION_FAILED for o in observations),no_hit=sum(o['status']==NO_HIT for o in observations),
                     width2_full=sum(o['full_window'] and o['status']==COMPLETE for o in observations),
                     legacy_approx=sum(not o['full_window'] and o['status']==COMPLETE for o in observations),
                     qcp_cap=tuner.qcp_budget.cap,qcp_used=tuner.qcp_budget.used,qcp_remaining=remaining,
                     root_qcp=tuner.qcp_budget.used-sum(o['qcp_used'] for o in observations),
                     exec_s=execution,exec_wall_s=ew,rec_s=rec,trans_s=transition,total_s=execution+rec+transition,
                     explain_s=(meter.explain_ms-x0)/1000,scope_exclusive_s=(meter.total_evaluate_ms-s0-meter.explain_ms+x0)/1000,
                     other_rec_s=rec-(meter.total_evaluate_ms-s0)/1000,
                     changed=int(original_old!=manager.current),creates=len(target-before_transition),drops=len(before_transition-target),
                     timeouts=len(errors),reset=int(tuner.workload_count<before_round+1),stranded_budget=0,
                     ordinary_order=json.dumps(getattr(tuner,'ordinary_order',[])),
                     structural_priority=json.dumps(tuner.structural_priority),outcomes=json.dumps(compact),wall_s=time.monotonic()-r0)
            if writer is None:writer=csv.DictWriter(stream,fieldnames=list(row));writer.writeheader()
            writer.writerow(row);stream.flush();rows.append(row)
            print(json.dumps(dict(workload=name,window=i+1,expected=len(windows),qcp=meter.attempts)),flush=True)
        status='COMPLETE'
    except BaseException as exc:
        append(d/'error_events.jsonl',dict(event='run_failed',type=type(exc).__name__,error=str(exc)))
        raise
    finally:
        cleanup0=time.monotonic()
        try:
            cleanup=manager.cleanup()
            final=make_session(measure,[('public',t) for t in tables],meter)
            final_epoch=final._capture_epoch(final.relevant_relations)
            assert same_snapshot(reference,final_epoch),'FINAL_DATABASE_IDENTITY_CHANGED'
            manifest['final_epoch']=final_epoch
        finally:
            c.close();measure.close();meta.close();stream.close()
        cleanup_s=time.monotonic()-cleanup0;trans_wall+=cleanup_s;wall=time.monotonic()-started
        rec=wall-exec_wall-trans_wall
        manifest.update(status=status,rounds=len(rows),exec_s=exec_total,exec_wall_s=exec_wall,rec_s=rec,trans_s=trans_wall,
                        total_s=exec_total+rec+trans_wall,wall_s=wall,explain_s=meter.explain_ms/1000,
                        scope_exclusive_s=(meter.total_evaluate_ms-meter.explain_ms)/1000,
                        other_rec_s=rec-meter.total_evaluate_ms/1000,cleanup_s=cleanup_s,
                        qcp_used=meter.attempts,qcp_cap=sum(case['qcp_budget_per_window']),qcp_failures=meter.failures,
                        exact_restoration=cleanup['exact_restoration'])
        for key in ('changed','creates','drops','timeouts','reset','nominated','complete','deferred','failed','no_hit','width2_full','legacy_approx','stranded_budget'):
            manifest[key]=sum(r[key] for r in rows)
        write_json(d/'run_manifest.json',manifest)
    assert status=='COMPLETE'


def campaign(out):
    m=load(out);limits=m['profiles']['live_limits'];start=time.time();deadline=start+limits['campaign_wall_s']
    assert 'started_unix' not in m,'NO_AUTOMATIC_RESTART'
    m.update(started_unix=start,deadline_unix=deadline,status='RUNNING',completed_runs=0);write_json(out/'run_manifest.json',m)
    completed=[];status='INCOMPLETE'
    try:
        jobs=[['preflight']]+[['run',c['name']] for c in m['profiles']['cases']]
        for job in jobs:
            assert time.time()<deadline,'CAMPAIGN_DEADLINE'
            cmd=[sys.executable,'-m','adasel.main','--output',str(out),'--deadline',str(deadline),*job]
            with (out/'campaign.log').open('a') as log:
                worker=subprocess.Popen(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                receipt=read(out/'run_manifest.json');receipt['active_worker']=dict(pid=worker.pid,command=cmd)
                write_json(out/'run_manifest.json',receipt)
                try:code=worker.wait(timeout=max(1,deadline-time.time()))
                except subprocess.TimeoutExpired:
                    os.killpg(worker.pid,signal.SIGTERM)
                    try:code=worker.wait(timeout=limits['cleanup_grace_s'])
                    except subprocess.TimeoutExpired:os.killpg(worker.pid,signal.SIGKILL);worker.wait();code=-9
                if code:
                    if job[0]=='run':recover_owned(m,out/'runs'/job[1],job[1].split('_')[0])
                    raise RuntimeError('WORKER_FAILED:'+str(job)+':'+str(code))
            if job[0]=='run':
                r=read(out/'runs'/job[1]/'run_manifest.json')
                assert r['status']=='COMPLETE' and r['exact_restoration']
                completed.append(r)
                assert sum(x['qcp_used'] for x in completed)<=22271
                receipt=read(out/'run_manifest.json');receipt['completed_runs']=len(completed)
                write_json(out/'run_manifest.json',receipt)
        assert len(completed)==9 and sum(x['rounds'] for x in completed)==603
        status='COMPLETE'
    finally:
        receipt=read(out/'run_manifest.json')
        recorded_runs=[read(p) for p in (out/'runs').glob('*/run_manifest.json')]
        receipt.update(status=status,completed_runs=len(completed),completed_rounds=sum(x['rounds'] for x in completed),
                       wall_s=time.time()-start,qcp_used=sum(x.get('qcp_used',0) for x in recorded_runs),
                       qcp_counters_complete=all('qcp_used' in x for x in recorded_runs),automatic_followup='STOP')
        write_json(out/'run_manifest.json',receipt)
