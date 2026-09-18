"""Registered 18-run Legacy/M2 QCP screening. No action selector or scheduler."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import MethodType

ROOT = Path(__file__).resolve().parents[1]
DIAG = Path('/home/lx/experiments/mask_diagnostic_v2_live_20260917_135639')
SUPPORT_HASH = '78382edcbf93cfa8c110a06253f7f3c604f3da81fd76cfdab08e7f39a023573f'
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(DIAG/'tool'))


def read(p): return json.loads(Path(p).read_text())
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p, value):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def load(out):
    m=read(out/'registration.json')
    assert sha(DIAG/'tool/live_support.py')==SUPPORT_HASH, 'ADAPTER_IDENTITY_MISMATCH'
    assert sha(DIAG/'tool/mask_diagnostic.py')=='b3343cc66ee240feff2235761100702e4774cb5db20dbeca10591bf27f2b9fb3'
    assert sha(DIAG/'run_manifest.json')==m['contract']['source_manifest_sha256']
    for file,h in m['release']['files'].items():
        assert sha(ROOT/file)==h, 'FROZEN_PATCH_CHANGED:'+file
    # Preserve imports of the Legacy policy; expose only the pinned safe backend modules.
    from adasel.ada_select import AdaSelect
    import database, adaselect_pp
    old=read(DIAG/'run_manifest.json');transport=Path(old['transport_source'])
    for item in old['native_source_checks']:
        assert sha(Path(old['frozen_source'])/item['path'])==item['sha256'], 'ORIGINAL_SOURCE_CHANGED'
    assert Path(sys.modules[AdaSelect.__module__].__file__).resolve()==ROOT/'adasel/ada_select.py'
    for file,h in old['transport_source_checks'].items():
        assert hashlib.sha256((transport/file).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==h
    database.__path__.append(str(transport/'database'))
    adaselect_pp.__path__.append(str(transport/'adaselect_pp'))
    sys.path.append(str(transport))
    # tools is a namespace package; only evaluation_substrate is loaded from the transport.
    assert not any(k in sys.modules for k in ('adaselect_pp.action_runtime','adaselect_pp.action_dispatch','adaselect_pp.legacy_budget'))
    return m, AdaSelect


def schema(c):
    from live_support import sqlrows
    d=defaultdict(list)
    for t,col in sqlrows(c,"SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND table_name IN (SELECT tablename FROM pg_tables WHERE schemaname='public') ORDER BY table_name,ordinal_position"):
        d[t].append(col)
    return dict(d)


def config(items):
    from tools.evaluation_substrate.schema import ConfigurationSpec
    return ConfigurationSpec(tuple(sorted(set(items))))


def boot_meter(out, case, deadline, cap):
    from live_support import Meter, CampaignStop
    from adasel.measurement import QCPBudget
    class CommonMeter(Meter):
        def __init__(self):
            super().__init__(out,'policy',case,max(1,deadline-time.time()))
            self.deadline=time.monotonic()+max(0,deadline-time.time())
            self.budget=QCPBudget(cap);self.rows=[];self.current_window=-1
        def begin(self):
            self.check()
            try:self.budget.charge()  # Exactly one charge immediately before each EXPLAIN attempt.
            except AssertionError as exc:raise CampaignStop('QCP_ACCOUNTING_ERROR') from exc
            self.attempts+=1
            token=self.attempts
            self.rows.append(dict(id=token,window=self.current_window,purpose=self.purpose,
                                 query=dict(self.query_info),status='STARTED'))
            return token
        def end(self,token,start,error=None):
            elapsed=(time.monotonic()-start)*1000
            self.explain_ms+=elapsed;self.failures+=int(error is not None)
            self.rows[token-1].update(elapsed_ms=elapsed,status='FAILED' if error else 'OK',
                                      error=str(error) if error else None)
    return CommonMeter()


def facade_class():
    from live_support import NativeDatabase, CampaignStop, native_tuple
    from tools.evaluation_substrate import QuerySpec, IndexDefinition
    from adaselect_pp.common import sql_only
    class Facade(NativeDatabase):
        root_observation=None
        def calculate_now_cost(self,workload):
            total=0.
            for line in workload:
                self.session_meter.check()
                q=QuerySpec(str(self.session_meter.attempts),sql_only(line))
                try: result=self.session._evaluate(q,config(self.target))
                except Exception as exc:
                    # These are safety/identity failures, never a legacy zero fallback.
                    from tools.evaluation_substrate.reveal import OptimizerRevealError
                    if isinstance(exc,OptimizerRevealError):raise CampaignStop('SAFETY_OR_IDENTITY_FAILURE:'+str(exc)) from exc
                    raise
                total+=result.optimizer_cost
            return total
        def validate_full_add_context(self,workload,costs,old,idx):
            root=self.root_observation
            if root is None:return False
            if (tuple(workload),tuple(costs),frozenset(old))!=root[:3]:return False
            expected={IndexDefinition(t,tuple(cs)) for t,cs in old}|set(self.session.protected)
            if expected!=set(self.session.physical) or self.target!=expected:return False
            epoch=self.session._capture_epoch(self.session.relevant_relations)
            return epoch['epoch_hash']==root[3]
    return Facade


def snapshot_equal(before,after):
    # Physical owned indexes legitimately change; database, data/statistics and schema must not.
    return all(before[k]==after[k] for k in ('database_environment','statistics_fingerprint','schema_fingerprint'))


def preflight(out,deadline):
    m,_=load(out)
    from live_support import connect, make_session, catalog, qid, OwnedRoot, allowed_vocabulary
    from tools.evaluation_substrate import QuerySpec,IndexDefinition
    receipts=[];meter=boot_meter(out,'preflight',deadline,30)
    for db in ('tpch','tpchs','job'):
        c=connect(m,db,'preflight');sc=schema(c);before=catalog(c)
        manager=OwnedRoot(c,m,db,out/'preflight'/db,allowed_vocabulary(sc))
        expected={(r['table'],r['name'],r['definition']) for r in m['expected_indexes'][db]}
        assert {(r[1],r[2],r[3]) for r in before}==expected and all(r[4] or r[5] for r in before)
        s=make_session(c,[('public',t) for t in sc],meter)
        epoch=s._capture_epoch(s.relevant_relations)
        # Accepted diagnostic checkpoint supplies the exact database/schema/statistics identity.
        import gzip
        cp=next((DIAG/'measure'/(db+'_shifting')/'checkpoints').glob('*.json.gz'))
        old=json.load(gzip.open(cp,'rt'))['measurement_epoch']
        assert snapshot_equal(old,epoch), 'SOURCE_SNAPSHOT_IDENTITY_MISMATCH:'+db
        table=next(t for t in sc if len(sc[t])>=2)
        added=IndexDefinition(table,tuple(sc[table][:2]));target=config(set(s.physical)|{added})
        cases=[('root','SELECT 1',config(s.physical),None,None),
               ('add','SELECT 1',target,None,None),
               ('exception','SELECT width2_missing_preflight_column FROM public.'+qid(table),target,None,'42703'),
               ('timeout','SELECT count(*) FROM '+','.join('public.'+qid(table)+' '+qid('a'+str(i)) for i in range(16)),target,'timeout','57014'),
               ('root_recheck','SELECT 1',config(s.physical),None,None)]
        checks=[]
        try:
            for name,sql,tgt,fault,expected_error in cases:
                meter.forced_failure=fault;error=None
                try:s._evaluate(QuerySpec(name,sql),tgt)
                except Exception as exc:error=getattr(exc,'pgcode',str(exc))
                finally:meter.forced_failure=None
                assert error==expected_error,(db,name,error,expected_error)
                s._assert_clean();assert catalog(c)==before
                checks.append(dict(test=name,error=error,clean=True))
            after=s._capture_epoch(s.relevant_relations)
            assert epoch['epoch_hash']==after['epoch_hash']
            receipts.append(dict(database=db,checks=checks,epoch=epoch,exact_restoration=True))
        finally:
            s._assert_clean();assert catalog(c)==before;manager.cleanup();c.close()
    write(out/'preflight.json',dict(status='PASS',attempts=meter.attempts,ledger=meter.rows,databases=receipts))
    assert meter.attempts<=30


def run_one(out,name,arm,deadline):
    started=time.monotonic();m,AdaSelect=load(out)
    from live_support import connect, OwnedRoot, allowed_vocabulary, make_session, native_tuple, CampaignStop
    from adasel.measurement import QCPBudget
    from adaselect_pp.common import sql_only
    from tools.evaluation_substrate.reveal import _safe_explain_query
    case=next(c for c in m['contract']['cases'] if c['name']==name);db=case['benchmark']
    directory=out/'runs'/(name+'_'+arm);directory.mkdir(parents=True,exist_ok=False)
    logging.basicConfig(filename=directory/'policy.log',level=logging.INFO,encoding='utf-8')
    c=connect(m,db,'runtime-'+arm);measure=connect(m,db,'measure-'+arm);meta=connect(m,db,'schema-'+arm)
    tables=schema(c);manager=OwnedRoot(c,m,db,directory,allowed_vocabulary(tables))
    meter=boot_meter(directory,name,deadline,0);manager.meter=meter
    facade=facade_class()(meta,manager);facade.session_meter=meter
    settings=dict(case['config'],width2_add_full_window=arm=='M2Q')
    tuner=AdaSelect(db,facade,facade,facade,cfg_source=settings)
    write(directory/'effective_config.json',tuner._cfg_effective)
    observations=[];rounds=[];exec_wall=0.;trans_wall=0.;runtime_total=0.;status='RUNNING'
    original_initial=tuner._initial_costs
    def initial(self,window):
        meter.purpose='root'
        assert meter.budget.fits(len(window)), 'ROOT_CANNOT_FIT_REGISTERED_CAP'
        epoch=facade.session._capture_epoch(facade.session.relevant_relations)
        costs,total=original_initial(window)
        after=facade.session._capture_epoch(facade.session.relevant_relations)
        assert epoch['epoch_hash']==after['epoch_hash'], 'ROOT_EPOCH_CHANGED'
        facade.root_observation=(tuple(window),tuple(costs),frozenset(manager.current),epoch['epoch_hash'])
        return costs,total
    tuner._initial_costs=MethodType(initial,tuner)
    original_test=tuner._test_candidate
    def test(self,idx,masks,costs,total,old,window):
        meter.purpose='KEEP' if idx in old else 'ADD';q0=meter.attempts
        result=original_test(idx,masks,costs,total,old,window)
        row=dict(self._last_candidate_observation,window=meter.current_window,role=meter.purpose,
                 actual_qcp=meter.attempts-q0,nomination_order=len(observations))
        observations.append(row)
        if result=='CONTEXT_INVALID':raise CampaignStop('CONTEXT_INVALID')
        return result
    tuner._test_candidate=MethodType(test,tuner)
    original_diag=tuner._record_structural_pair_replacement_diagnostic
    def diagnostic(self,*args):
        meter.purpose='legacy_replacement_diagnostic'
        return original_diag(*args)
    tuner._record_structural_pair_replacement_diagnostic=MethodType(diagnostic,tuner)
    # No fixture or diagnostic costs are accessible from the policy facade.
    trace=Path(case['workload']);assert sha(trace)==case['workload_sha256']
    lines=[s.strip() for s in trace.read_text().splitlines() if s.strip()]
    windows=[lines[i:i+case['round_size']] for i in range(0,len(lines),case['round_size'])]
    assert len(windows)==case['rounds']
    reference_epoch=next(r['epoch'] for r in read(out/'preflight.json')['databases'] if r['database']==db)
    def terminate(signum,frame):raise CampaignStop('SUPERVISOR_DEADLINE')
    signal.signal(signal.SIGTERM,terminate)
    try:
        for r,window in enumerate(windows):
            meter.check();runtimes=[];errors=[];e0=time.monotonic()
            for i,line in enumerate(window):
                meter.check()
                try:
                    with c.cursor() as q:
                        timeout=min(case['config']['timeout'],max(1,int((meter.deadline-time.monotonic())*1000)))
                        q.execute('SELECT set_config(%s,%s,false)',('statement_timeout',str(timeout)))
                        q.execute('EXPLAIN (ANALYZE, FORMAT JSON) '+_safe_explain_query(sql_only(line)))
                        payload=q.fetchone()[0]
                    rt=float(payload[0]['Plan']['Actual Total Time'])
                except Exception as exc:
                    rt=30000.;errors.append(dict(occurrence=i,error=str(exc),sqlstate=getattr(exc,'pgcode',None)))
                runtimes.append(rt)
            ew=time.monotonic()-e0;exec_wall+=ew;execution=sum(runtimes)/1000;runtime_total+=execution
            rec0=time.monotonic();reset_before=manager.ddl_ms
            session=make_session(measure,[('public',t) for t in tables],meter);facade.bind(session)
            epoch=session._capture_epoch(session.relevant_relations)
            assert snapshot_equal(reference_epoch,epoch),'DATABASE_IDENTITY_CHANGED'
            assert {native_tuple(i) for i in set(session.physical)-session.protected}==manager.current
            old=set(manager.current);saved_old=set(old)
            meter.current_window=r;meter.budget=QCPBudget(case['qcp_budget_per_window'][r]);facade.qcp_budget=meter.budget
            q0=meter.attempts;x0=meter.explain_ms;s0=meter.total_evaluate_ms;obs0=len(observations)
            target=tuner.run(window,old,[int(v) for v in runtimes])
            assert len(target)<=case['config']['max_num'] and set(target)<=set(tuner.columns_benefit)|old
            session._assert_clean();post_epoch=session._capture_epoch(session.relevant_relations)
            assert snapshot_equal(reference_epoch,post_epoch),'DATABASE_IDENTITY_CHANGED'
            reset_s=(manager.ddl_ms-reset_before)/1000
            recommendation=time.monotonic()-rec0-reset_s;trans_wall+=reset_s
            topk={k for k,v in sorted(tuner._last_net_benefit_map.items(),key=lambda kv:kv[1],reverse=True)[:tuner.max_num]}
            for row in observations[obs0:]:
                idx=row['index'];row.update(in_topk=idx in topk,in_candidate_conf=idx in tuner._last_candidate_conf,
                    after_gate=idx in target,beta_rejected=idx in tuner._last_candidate_conf and idx not in target,
                    materialized_new=idx in target and idx not in manager.current)
            trans0=time.monotonic();before_transition=set(manager.current)
            manager.transition(target,'LEGACY_SELECTOR_RETURNED_CONFIGURATION')
            transition=time.monotonic()-trans0+reset_s;trans_wall+=transition-reset_s
            assert meter.attempts-q0==meter.budget.used<=meter.budget.cap
            remaining=meter.budget.cap-meter.budget.used
            assert all(x['required_qcp']>remaining for x in observations[obs0:] if x['status']=='BUDGET_DEFER')
            assert all(d['required_qcp']>remaining for d in tuner._last_structural_pair_replacement_map.values()
                       if d.get('replacement_obs_src')=='BUDGET_DEFER')
            diagnostic_deferred=sum(d.get('replacement_obs_src')=='BUDGET_DEFER' for d in tuner._last_structural_pair_replacement_map.values())
            row=dict(workload=name,arm=arm,window=r,exec_s=execution,exec_wall_s=ew,rec_s=recommendation,trans_s=transition,
                     total_s=execution+recommendation+transition,qcp_cap=meter.budget.cap,qcp_used=meter.budget.used,
                     changed=int(saved_old!=target),creates=len(target-before_transition),drops=len(before_transition-target),
                     timeouts=len(errors),runtime_errors=errors,budget_deferred=sum(x['status']=='BUDGET_DEFER' for x in observations[obs0:]),
                     diagnostic_deferred=diagnostic_deferred,explain_s=(meter.explain_ms-x0)/1000,
                     hypopg_scope_s=(meter.total_evaluate_ms-s0-meter.explain_ms+x0)/1000,
                     remaining_qcp=meter.budget.cap-meter.budget.used,old=sorted(saved_old),target=sorted(target),
                     nominated=[x['index'] for x in observations[obs0:]],evaluated=sorted(tuner._last_evaluated_set))
            rounds.append(row)
            write(directory/'progress.json',dict(window=r+1,expected=len(windows),qcp=meter.attempts))
            print(json.dumps(dict(workload=name,arm=arm,window=r+1,qcp=meter.attempts)),flush=True)
        status='COMPLETE'
    except BaseException as exc:
        status='INCOMPLETE';write(directory/'failure.json',dict(type=type(exc).__name__,message=str(exc)))
        raise
    finally:
        cleanup0=time.monotonic()
        try:
            cleanup=manager.cleanup()
            final=make_session(measure,[('public',t) for t in tables],meter)
            final_epoch=final._capture_epoch(final.relevant_relations)
            assert snapshot_equal(reference_epoch,final_epoch),'FINAL_DATABASE_IDENTITY_CHANGED'
            write(directory/'final_epoch.json',final_epoch)
        finally:
            c.close();measure.close();meta.close()
        cleanup_s=time.monotonic()-cleanup0;trans_wall+=cleanup_s
        write(directory/'rounds.json',rounds);write(directory/'observations.json',observations);write(directory/'qcp_ledger.json',meter.rows)
        wall=time.monotonic()-started
        recommendation=wall-exec_wall-trans_wall
        summary=dict(workload=name,arm=arm,status=status,rounds=len(rounds),expected_rounds=len(windows),
                     exec_s=runtime_total,exec_wall_s=exec_wall,rec_s=recommendation,trans_s=trans_wall,
                     total_s=runtime_total+recommendation+trans_wall,wall_s=wall,qcp_used=meter.attempts,
                     qcp_cap=sum(case['qcp_budget_per_window']),qcp_failures=meter.failures,
                     config_change_rounds=sum(r['changed'] for r in rounds),creates=sum(r['creates'] for r in rounds),
                     drops=sum(r['drops'] for r in rounds),timeouts=sum(r['timeouts'] for r in rounds),
                     budget_deferred=sum(r['budget_deferred'] for r in rounds),diagnostic_deferred=sum(r['diagnostic_deferred'] for r in rounds),
                     explain_s=meter.explain_ms/1000,hypopg_scope_s=(meter.total_evaluate_ms-meter.explain_ms)/1000,
                     other_recommendation_s=recommendation-meter.total_evaluate_ms/1000,cleanup_s=cleanup_s,
                     run_bookkeeping_s=recommendation-sum(r['rec_s'] for r in rounds),
                     exact_restoration=cleanup['exact_restoration'],control_label='common-QCP cutoff wrapper control' if arm=='LQ' else 'width2 full ADD + common-QCP cutoff')
        write(directory/'summary.json',summary)
    assert status=='COMPLETE'


def campaign(out):
    m,_=load(out);contract=m['contract'];start=time.time();deadline=start+contract['time_caps']['matrix_wall_seconds']
    assert not (out/'campaign_started.json').exists(),'NO_AUTOMATIC_RESTART'
    write(out/'campaign_started.json',dict(started_unix=start,deadline_unix=deadline,max_runs=18,cleanup_seconds=900))
    completed=[];status='INCOMPLETE'
    try:
        commands=[['preflight']]+[['run',item['workload'],arm] for item in contract['execution_order'] for arm in item['arms']]
        for parts in commands:
            assert time.time()<deadline,'CAMPAIGN_DEADLINE'
            cmd=[sys.executable,str(Path(__file__).resolve()),'--output',str(out),'--deadline',str(deadline),*parts]
            with (out/'campaign.log').open('a') as log:
                worker=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                write(out/'active_worker.json',dict(pid=worker.pid,command=cmd,completed_runs=len(completed)))
                try: code=worker.wait(timeout=max(1,deadline-time.time()))
                except subprocess.TimeoutExpired:
                    os.killpg(worker.pid,signal.SIGTERM)
                    try:code=worker.wait(timeout=contract['time_caps']['cleanup_grace_seconds'])
                    except subprocess.TimeoutExpired:
                        os.killpg(worker.pid,signal.SIGKILL);worker.wait();code=-9
                if code:
                    if parts[0]=='run':
                        from live_support import recover_owned
                        recover_owned(m,out/'runs'/(parts[1]+'_'+parts[2]),parts[1].split('_')[0])
                    raise RuntimeError('WORKER_FAILED:'+str(parts)+':'+str(code))
            if parts[0]=='run':
                result=read(out/'runs'/(parts[1]+'_'+parts[2])/'summary.json')
                assert result['status']=='COMPLETE' and result['exact_restoration']
                completed.append(result)
                assert sum(r['qcp_used'] for r in completed)<=44542
                write(out/'progress.json',dict(status='RUNNING',completed_runs=len(completed),runs=completed))
        assert len(completed)==18 and sum(r['rounds'] for r in completed)==1206
        status='COMPLETE'
    except BaseException as exc:
        write(out/'campaign_failure.json',dict(type=type(exc).__name__,message=str(exc)))
        raise
    finally:
        write(out/'campaign_result.json',dict(status=status,completed_runs=len(completed),runs=completed,
                                             wall_s=time.time()-start,automatic_followup='STOP'))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--deadline',type=float)
    ap.add_argument('mode',choices=['campaign','preflight','run']);ap.add_argument('workload',nargs='?');ap.add_argument('arm',nargs='?')
    a=ap.parse_args()
    if a.mode=='campaign':campaign(a.output)
    elif a.mode=='preflight':preflight(a.output,a.deadline)
    else:run_one(a.output,a.workload,a.arm,a.deadline)


if __name__=='__main__':main()
