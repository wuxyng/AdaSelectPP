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
from database.execution_errors import classify_error
from experiments.core_v2.receipts import ReceiptMeter, qcp_receipt, write_json


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
    """Keep the registered SQL/timeout; only a server statement timeout is recoverable."""
    stage='validation';meter.runtime_stage=stage;meter.runtime_request_intent=False;meter.runtime_response_confirmed=False
    try:
        statement='EXPLAIN (ANALYZE, FORMAT JSON) '+_safe_explain_query(sql_only(line))
        meter.check()
        timeout=min(timeout_ms,max(1,int((meter.deadline-time.monotonic())*1000)))
        stage='setup';meter.runtime_stage=stage
        with connection.cursor() as cursor:
            cursor.execute('SELECT set_config(%s,%s,false)',('statement_timeout',str(timeout)))
            stage='execute';meter.runtime_stage=stage;meter.runtime_request_intent=True
            token=meter.begin() if getattr(meter,'kind',None)=='preflight' else None
            start=time.monotonic();error=None
            try:
                cursor.execute(statement)
                meter.runtime_response_confirmed=True
                plan=cursor.fetchone()[0]
            except BaseException as exc:
                if getattr(exc,'pgcode',None):meter.runtime_response_confirmed=True
                error=exc
                raise
            finally:
                if token is not None:meter.end(token,start,error)
        stage='observation';meter.runtime_stage=stage
        if isinstance(plan,str):plan=json.loads(plan)
        runtime=float(plan[0]['Plan']['Actual Total Time'])
        if not math.isfinite(runtime) or runtime<0:
            raise CampaignStop('INVALID_RUNTIME_OBSERVATION')
        return runtime,None
    except BaseException as exc:
        event=classify_error(exc,stage)
        if not event['fatal']:return event['protocol_penalty_ms'],event
        if isinstance(exc,(KeyboardInterrupt,CampaignStop)):
            exc.execution_event=event
            raise
        failure=CampaignStop('EXECUTION_DRIVER_OR_CONNECTION_FAILURE:'+str(exc))
        failure.execution_event=event
        raise failure from exc


def reference_epoch(m,db):
    item=m['snapshot_checkpoints'][db]
    assert sha(item['path'])==item['sha256'],'REFERENCE_CHECKPOINT_CHANGED'
    return json.load(gzip.open(item['path'],'rt'))['measurement_epoch']


def preflight(out,deadline):
    m=load(out);meter=ReceiptMeter(deadline,out/'preflight'/'qcp_receipt.json')
    meter.kind='preflight';meter.budget=QCPBudget(30);receipts=[];status='INCOMPLETE'
    try:
        for db in ('tpch','tpchs','job'):
            c=None;manager=None;session=None;primary=None;errors=[];checks=[]
            try:
                c=connect(m,db,'preflight');sc=schema(c);before=catalog(c)
                manager=OwnedRoot(c,m,db,out/'preflight'/db,allowed_vocabulary(sc))
                # This is the same runtime entry, validation, SQL and JSON parser as run_one.
                # Run before make_session changes the connection transaction mode.
                runtime,error=execute_occurrence(c,'SELECT 1',30000,meter)
                assert error is None,'PREFLIGHT_RUNTIME_FAILED'
                checks.append(dict(test='execute_occurrence',observed_ms=runtime,error=None))
                session=make_session(c,[('public',t) for t in sc],meter)
                epoch=session._capture_epoch(session.relevant_relations)
                assert same_snapshot(reference_epoch(m,db),epoch),'SOURCE_SNAPSHOT_IDENTITY_MISMATCH'
                table=next(t for t in sc if len(sc[t])>=2)
                target=config(set(session.physical)|{IndexDefinition(table,tuple(sc[table][:2]))})
                cases=[('root','SELECT 1',config(session.physical),None,None),
                       ('add','SELECT 1',target,None,None),
                       ('exception','SELECT core_missing_preflight_column FROM public.'+qid(table),target,None,'42703'),
                       ('timeout','SELECT count(*) FROM '+','.join('public.'+qid(table)+' '+qid('a'+str(i)) for i in range(16)),target,'timeout','57014'),
                       ('root_recheck','SELECT 1',config(session.physical),None,None)]
                for name,sql,tgt,fault,expected in cases:
                    meter.forced_failure=fault;error=None
                    try:session._evaluate(QuerySpec(name,sql),tgt)
                    except Exception as exc:error=getattr(exc,'pgcode',str(exc))
                    finally:meter.forced_failure=None
                    assert error==expected,(db,name,error,expected)
                    session._assert_clean();assert catalog(c)==before
                    checks.append(dict(test=name,error=error,clean=True))
                assert session._capture_epoch(session.relevant_relations)['epoch_hash']==epoch['epoch_hash']
            except BaseException as exc:primary=exc
            finally:
                if session is not None:
                    try:session._assert_clean();assert catalog(c)==before
                    except BaseException as exc:errors.append(classify_error(exc,'preflight_validation'))
                if manager is not None:
                    try:manager.cleanup()
                    except BaseException as exc:errors.append(classify_error(exc,'preflight_cleanup'))
                if c is not None:
                    try:c.close()
                    except BaseException as exc:errors.append(classify_error(exc,'preflight_close'))
                receipts.append(dict(database=db,checks=checks,exact_restoration=manager is not None and not errors,
                                     failure=classify_error(primary,'preflight') if primary else None,secondary_errors=errors))
            if primary is not None:raise primary
            if errors:raise CampaignStop('PREFLIGHT_FINALIZATION_FAILED:'+str(errors))
        assert meter.attempts<=30
        status='PASS'
    finally:
        m['preflight']=dict(status=status,attempts=meter.attempts,actual_attempts=meter.confirmed if not meter.uncertain and meter.inflight is None else None,
                            runtime_checks=sum(c['test']=='execute_occurrence' for r in receipts for c in r['checks']),databases=receipts)
        write_json(out/'run_manifest.json',m)


def run_one(out,name,deadline):
    started=time.monotonic();m=load(out)
    case=next(c for c in m['profiles']['cases'] if c['name']==name);db=case['benchmark']
    d=out/'runs'/name;d.mkdir(parents=True,exist_ok=False)
    connections=[];manager=None;stream=None;meter=None;reference=None;rows=[]
    exec_wall=0.;exec_total=0.;observed_total=0.;penalty_total=0.;trans_wall=0.;cleanup_s=0.
    runtime_started=0;runtime_confirmed=0;runtime_completed=0;runtime_failed=0
    status='INCOMPLETE';primary=None;secondary=[];restored=None
    manifest=dict(revision=m['release']['commit'],workload=name,policy_inputs=case['policy_inputs'],
                  qcp_budget_per_window=case['qcp_budget_per_window'],constants=dict(POLICY_DEFAULTS),
                  workload_sha256=case['workload_sha256'],query_timeout_ms=case['query_timeout_ms'],
                  status='RUNNING',time_definition=m['time_definition'],rounds=0,
                  qcp_cap=sum(case['qcp_budget_per_window']),resources_complete=False,
                  accounting_definition='exec_s = observed Plan Actual Total Time + separately identified historical timeout penalties; QCP used is budget charges, actual attempts may be unknown')

    def checkpoint(final=False):
        wall=time.monotonic()-started;rec=wall-exec_wall-trans_wall
        complete_counter=meter is not None and meter.inflight is None and meter.uncertain==0
        manifest.update(status=status if final else 'RUNNING',rounds=len(rows),
                        exec_s=exec_total,exec_observed_s=observed_total,exec_protocol_penalty_s=penalty_total,
                        exec_wall_s=exec_wall,rec_s=rec,trans_s=trans_wall,total_s=exec_total+rec+trans_wall,
                        wall_s=wall,cleanup_s=cleanup_s,resources_complete=final,
                        exec_observations_complete=runtime_failed==0 and penalty_total==0,
                        round_counters_cover_completed_rows_only=True,
                        runtime_request_intents=runtime_started,runtime_confirmed_requests=runtime_confirmed,
                        runtime_actual_requests=runtime_confirmed if final and runtime_confirmed==runtime_started else None,
                        runtime_request_count_complete=final and runtime_confirmed==runtime_started,
                        runtime_completed=runtime_completed,runtime_failed=runtime_failed,
                        qcp_used=meter.attempts if meter else 0,qcp_budget_charge=meter.attempts if meter else 0,
                        qcp_actual_attempts=meter.attempts if complete_counter else None,
                        qcp_counters_complete=complete_counter,
                        qcp_confirmed_lower_bound=meter.confirmed if meter else 0,
                        qcp_failures=meter.failures if meter else 0,
                        explain_s=meter.explain_ms/1000 if meter else 0.,
                        scope_exclusive_s=(meter.total_evaluate_ms-meter.explain_ms)/1000 if meter else 0.,
                        other_rec_s=rec-meter.total_evaluate_ms/1000 if meter else rec,
                        exact_restoration=restored,secondary_errors=secondary)
        for key in ('changed','creates','drops','timeouts','reset','nominated','complete','deferred','failed','no_hit','width2_full','legacy_approx','stranded_budget'):
            manifest[key]=sum(r[key] for r in rows)
        write_json(d/'run_manifest.json',manifest)

    def error_record(exc,stage):
        event=classify_error(exc,stage)
        secondary.append(event)
        try:append(d/'error_events.jsonl',dict(event='secondary_failure',**event))
        except Exception as receipt_error:
            secondary.append(classify_error(receipt_error,'error_receipt'))

    def terminate(*_):raise CampaignStop('SUPERVISOR_TERMINATED')
    old_handler=signal.signal(signal.SIGTERM,terminate)
    try:
        checkpoint()
        meter=ReceiptMeter(deadline,d/'qcp_receipt.json')
        for role in ('runtime','measurement','schema'):
            connections.append(connect(m,db,role))
        c,measure,meta=connections
        tables=schema(c);manager=OwnedRoot(c,m,db,d,allowed_vocabulary(tables));manager.meter=meter
        backend=Backend(meta,manager);backend.session_meter=meter
        policy=PolicyConfig(**case['policy_inputs'],qcp_budget=case['qcp_budget_per_window'][0])
        tuner=AdaSelect(db,backend,policy,runtime=RuntimeConfig(timeout_ms=case['query_timeout_ms']))
        cost_keys=set(allowed_vocabulary(tables));model=tuner.benefit_norm
        missing=sum(k not in model.index_costs_by_key and
                    (k[1] not in model.index_costs or k[1] in model.creation_cost_collisions) for k in cost_keys)
        manifest.update(creation_cost_path=model.creation_cost_path,creation_cost_sha256=sha(ROOT/model.creation_cost_path),
                        creation_cost_missing=missing,creation_cost_allowlist_total=len(cost_keys),
                        creation_cost_missing_fraction=missing/max(1,len(cost_keys)))
        lines=[line.strip() for line in Path(case['workload_locator_hint']).read_text().splitlines() if line.strip()]
        windows=[lines[i:i+case['round_size']] for i in range(0,len(lines),case['round_size'])]
        assert len(windows)==case['rounds'] and len(lines)==case['query_occurrences']
        reference=reference_epoch(m,db)
        stream=(d/'rounds.csv').open('w',encoding='utf-8',newline='');writer=None
        for i,window in enumerate(windows):
            r0=time.monotonic();meter.check();runtimes=[];errors=[];error_positions=set();ew=0.
            for j,line in enumerate(window):
                manifest.update(active_phase='execution',active_window=i,active_occurrence=j)
                checkpoint();e0=time.monotonic();error=None
                try:
                    rt,error=execute_occurrence(c,line,case['query_timeout_ms'],meter)
                    runtimes.append(rt);exec_total+=rt/1000;runtime_completed+=1
                    if error is None:observed_total+=rt/1000
                    else:penalty_total+=rt/1000
                except BaseException as exc:
                    runtime_failed+=1
                    error=getattr(exc,'execution_event',classify_error(exc,getattr(meter,'runtime_stage','execution')))
                    raise
                finally:
                    elapsed=time.monotonic()-e0;ew+=elapsed;exec_wall+=elapsed
                    runtime_started+=int(getattr(meter,'runtime_request_intent',False))
                    runtime_confirmed+=int(getattr(meter,'runtime_response_confirmed',False))
                    if error is not None:
                        event=dict(window=i,occurrence=j,**error)
                        errors.append(event);error_positions.add(j);append(d/'error_events.jsonl',event)
                    checkpoint()
            execution=sum(runtimes)/1000
            manifest['active_phase']='recommendation';checkpoint()
            rec0=time.monotonic();reset_before=manager.ddl_ms
            try:
                session=make_session(measure,[('public',t) for t in tables],meter);backend.bind(session)
                epoch=session._capture_epoch(session.relevant_relations)
                assert same_snapshot(reference,epoch),'DATABASE_IDENTITY_CHANGED'
                assert {native_tuple(x) for x in set(session.physical)-session.protected}==manager.current
                old=set(manager.current);original_old=set(old);before_round=tuner.workload_count
                meter.current_window=i;q0=meter.attempts;x0=meter.explain_ms;s0=meter.total_evaluate_ms
                target=tuner.run(window,old,[int(v) for v in runtimes],window_id=i,qcp_budget=case['qcp_budget_per_window'][i])
                assert len(target)<=policy.max_num and set(target)<=set(tuner.columns_benefit)|old
                session._assert_clean();assert same_snapshot(reference,session._capture_epoch(session.relevant_relations))
            finally:
                reset_s=(manager.ddl_ms-reset_before)/1000
                trans_wall+=reset_s;rec=time.monotonic()-rec0-reset_s
            manifest['active_phase']='transition';checkpoint()
            t0=time.monotonic();before_transition=set(manager.current)
            try:manager.transition(target,'CORE_SELECTOR_RETURNED_CONFIGURATION')
            finally:
                elapsed=time.monotonic()-t0;transition=elapsed+reset_s;trans_wall+=elapsed
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
                     exec_s=execution,exec_observed_s=sum(runtimes[j] for j in range(len(runtimes)) if j not in error_positions)/1000,
                     exec_protocol_penalty_s=sum(e['protocol_penalty_ms'] for e in errors)/1000,exec_wall_s=ew,rec_s=rec,trans_s=transition,total_s=execution+rec+transition,
                     explain_s=(meter.explain_ms-x0)/1000,scope_exclusive_s=(meter.total_evaluate_ms-s0-meter.explain_ms+x0)/1000,
                     other_rec_s=rec-(meter.total_evaluate_ms-s0)/1000,
                     changed=int(original_old!=manager.current),creates=len(target-before_transition),drops=len(before_transition-target),
                     timeouts=len(errors),reset=int(tuner.workload_count<before_round+1),stranded_budget=0,
                     ordinary_order=json.dumps(getattr(tuner,'ordinary_order',[])),
                     structural_priority=json.dumps(tuner.structural_priority),outcomes=json.dumps(compact),wall_s=time.monotonic()-r0)
            if writer is None:writer=csv.DictWriter(stream,fieldnames=list(row));writer.writeheader()
            writer.writerow(row);stream.flush();os.fsync(stream.fileno());rows.append(row)
            checkpoint()
            print(json.dumps(dict(workload=name,window=i+1,expected=len(windows),qcp=meter.attempts)),flush=True)
        status='COMPLETE'
    except BaseException as exc:
        primary=exc
        manifest['primary_error']=getattr(exc,'execution_event',classify_error(exc,manifest.get('active_phase','setup')))
        try:append(d/'error_events.jsonl',dict(event='run_failed',**manifest['primary_error']))
        except Exception as receipt_error:secondary.append(classify_error(receipt_error,'error_receipt'))
    finally:
        # A second SIGTERM must not interrupt restoration and erase the receipt.
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        cleanup0=time.monotonic();manifest['active_phase']='cleanup'
        try:
            if manager is not None:
                try:
                    cleanup=manager.cleanup();restored=cleanup['exact_restoration']
                except BaseException as exc:
                    restored=False;error_record(exc,'cleanup')
                if reference is not None:
                    try:
                        final=make_session(connections[1],[('public',t) for t in tables],meter)
                        final_epoch=final._capture_epoch(final.relevant_relations)
                        assert same_snapshot(reference,final_epoch),'FINAL_DATABASE_IDENTITY_CHANGED'
                        manifest['final_epoch']=final_epoch
                    except BaseException as exc:
                        restored=False;error_record(exc,'final_validation')
        finally:
            for role,connection in zip(('runtime','measurement','schema'),connections):
                try:connection.close()
                except BaseException as exc:error_record(exc,'close_'+role)
            if stream is not None:
                try:stream.close()
                except BaseException as exc:error_record(exc,'close_rounds')
            cleanup_s=time.monotonic()-cleanup0;trans_wall+=cleanup_s
            if secondary:status='INCOMPLETE'
            try:checkpoint(final=True)
            finally:signal.signal(signal.SIGTERM,old_handler)
    if primary is not None:raise primary
    if secondary:raise CampaignStop('FINALIZATION_FAILED:'+str(secondary))
    assert status=='COMPLETE'


def signal_worker(worker,force=False):
    if os.name=='nt':
        (worker.kill if force else worker.terminate)()
    else:
        try:os.killpg(worker.pid,signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:pass


def stop_worker(worker,grace):
    """Return only after the child is reaped. Never restore a live worker's DDL."""
    handlers={sig:signal.signal(sig,signal.SIG_IGN) for sig in (signal.SIGINT,signal.SIGTERM)}
    stop_deadline=time.monotonic()+grace
    try:
        signal_worker(worker)
        try:return worker.wait(timeout=max(0,grace-min(1.,grace/2)))
        except subprocess.TimeoutExpired:
            signal_worker(worker,force=True)
            return worker.wait(timeout=max(0,stop_deadline-time.monotonic()))
    finally:
        for sig,handler in handlers.items():signal.signal(sig,handler)


def campaign(out):
    m=load(out);limits=m['profiles']['live_limits'];start=time.time();deadline=start+limits['campaign_wall_s']
    assert 'started_unix' not in m,'NO_AUTOMATIC_RESTART'
    m.update(started_unix=start,deadline_unix=deadline,status='RUNNING',completed_runs=0,started_jobs=[])
    write_json(out/'run_manifest.json',m)
    completed=[];started_jobs=[];status='INCOMPLETE';supervisor_errors=[];recovery_s=0.
    def terminate(*_):raise CampaignStop('SUPERVISOR_TERMINATED')
    old_handler=signal.signal(signal.SIGTERM,terminate)
    try:
        jobs=[['preflight']]+[['run',c['name']] for c in m['profiles']['cases']]
        for job in jobs:
            assert time.time()<deadline,'CAMPAIGN_DEADLINE'
            cmd=[sys.executable,'-m','adasel.main','--output',str(out),'--deadline',str(deadline),*job]
            # Register intent before spawning: a missing child manifest is unknown,
            # not an unstarted/zero-cost run. Conservative overcharge is explicit.
            entry=dict(job=job,state='SPAWN_INTENT');started_jobs.append(entry)
            receipt=read(out/'run_manifest.json');receipt['started_jobs']=started_jobs
            write_json(out/'run_manifest.json',receipt)
            with (out/'campaign.log').open('a') as log:
                worker=None;exited=False;failure=None
                try:
                    pending_signals=[]
                    handlers={sig:signal.signal(sig,lambda signum,_:pending_signals.append(signum)) for sig in (signal.SIGINT,signal.SIGTERM)}
                    try:worker=subprocess.Popen(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    finally:
                        for sig,handler in handlers.items():signal.signal(sig,handler)
                    if pending_signals:raise CampaignStop('SUPERVISOR_INTERRUPTED_DURING_SPAWN')
                    entry.update(pid=worker.pid,state='RUNNING')
                    receipt=read(out/'run_manifest.json');receipt.update(active_worker=dict(pid=worker.pid,command=cmd),started_jobs=started_jobs)
                    write_json(out/'run_manifest.json',receipt)
                    code=worker.wait(timeout=max(1,deadline-time.time()));exited=True
                    if code:raise RuntimeError('WORKER_FAILED:'+str(job)+':'+str(code))
                    if job[0]=='run':
                        r=read(out/'runs'/job[1]/'run_manifest.json')
                        assert r['status']=='COMPLETE' and r['exact_restoration']
                except BaseException as exc:
                    failure=exc
                    if worker is not None and not exited:
                        try:stop_worker(worker,limits['cleanup_grace_s']);exited=True
                        except BaseException as stop_error:
                            supervisor_errors.append(classify_error(stop_error,'worker_stop'))
                    if exited:
                        t0=time.monotonic()
                        handlers={sig:signal.signal(sig,signal.SIG_IGN) for sig in (signal.SIGINT,signal.SIGTERM)}
                        try:
                            directories=[(out/'runs'/job[1],job[1].split('_')[0])] if job[0]=='run' else [(out/'preflight'/db,db) for db in ('tpch','tpchs','job')]
                            entry['recovery']=[recover_owned(m,path,db) for path,db in directories]
                        except BaseException as recovery_error:
                            supervisor_errors.append(classify_error(recovery_error,'parent_recovery'))
                        finally:
                            entry['parent_recovery_s']=time.monotonic()-t0
                            recovery_s+=entry['parent_recovery_s']
                            for sig,handler in handlers.items():signal.signal(sig,handler)
                    entry.update(state='EXITED' if exited else 'EXIT_UNCONFIRMED',failure=classify_error(failure,'supervisor'))
                    raise
                finally:
                    if failure is None:entry['state']='EXITED'
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
        try:
            receipt=read(out/'run_manifest.json');counters=[]
            for entry in started_jobs:
                if entry['job'][0]!='run':continue
                name=entry['job'][1];d=out/'runs'/name
                run=read(d/'run_manifest.json') if (d/'run_manifest.json').exists() else {}
                checkpoint=read(d/'qcp_receipt.json') if (d/'qcp_receipt.json').exists() else {}
                case=next(c for c in m['profiles']['cases'] if c['name']==name)
                counters.append(dict(workload=name,**qcp_receipt(run,sum(case['qcp_budget_per_window']),checkpoint)))
            known=bool(counters) and all(c['qcp_counters_complete'] for c in counters)
            if any(e['job'][0]=='preflight' for e in started_jobs):
                pre=receipt.get('preflight',{})
                pre_checkpoint=out/'preflight'/'qcp_receipt.json'
                pre_final=dict(status='INCOMPLETE',qcp_used=pre['attempts'],
                               qcp_actual_attempts=pre.get('actual_attempts'),qcp_counters_complete=pre.get('actual_attempts') is not None) if 'attempts' in pre else {}
                receipt['preflight_counters']=qcp_receipt(pre_final,30,read(pre_checkpoint) if pre_checkpoint.exists() else {})
            receipt.update(status=status,completed_runs=len(completed),completed_rounds=sum(x['rounds'] for x in completed),
                           wall_s=time.time()-start,qcp_used=sum(c['qcp_used'] for c in counters) if counters and all(c['qcp_used'] is not None for c in counters) else None,
                           qcp_actual_attempts=sum(c['qcp_actual_attempts'] for c in counters) if known else None,
                           qcp_budget_charge=sum(c['qcp_budget_charge'] for c in counters),qcp_counters_complete=known,
                           run_counters=counters,started_jobs=started_jobs,parent_recovery_s=recovery_s,
                           supervisor_errors=supervisor_errors,automatic_followup='STOP')
            receipt.pop('active_worker',None)
            write_json(out/'run_manifest.json',receipt)
        finally:signal.signal(signal.SIGTERM,old_handler)
