"""Offline measured-response regression through the final Core method, never live cache."""
import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests/core')]
from support import Backend,tuner,reference_class,history

FIELDS=('cohort','run','epoch','root_id','context_id','estimator','protocol','exact_sql_id','configuration_id')

def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def key(text):
    table,columns=text[:-1].split('(',1)
    return table,tuple(columns.split(','))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--diagnostic',type=Path,required=True)
    ap.add_argument('--package',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    contract=read(a.package/'EXPERIMENT_CONTRACT.json')
    assert hashlib.sha256((a.diagnostic/'run_manifest.json').read_bytes()).hexdigest()==contract['source_manifest_sha256']
    store={}
    for line in (a.diagnostic/'diagnostic_evidence.jsonl').read_text(encoding='utf-8').splitlines():
        row=json.loads(line)
        if row['quality']!='OK':continue
        k=tuple(row['key'][f] for f in FIELDS)
        assert math.isfinite(row['cost']) and row['cost']>=0
        assert k not in store or store[k]==row['cost']
        store[k]=row['cost']
    units={u['unit_id']:u for u in read(a.diagnostic/'sample_plan.json')['selected']}
    reference=reference_class();rows=[];hashes={};widths={1:0,2:0};recovered=0;max_error=0.
    with (a.package/'reference/expected_observations.csv').open(encoding='utf-8-sig') as f:refs=list(csv.DictReader(f))
    for expected in refs:
        rel=Path('measure')/expected['workload']/'checkpoints'/(expected['window']+'.json.gz')
        p=a.diagnostic/rel;cp=json.load(gzip.open(p,'rt',encoding='utf-8'));hashes[rel.as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        assert cp['checkpoint_id']==expected['checkpoint_id'] and cp['root_id']==expected['root_id'] and cp['epoch']==expected['epoch']
        assert cp['native_baseline_commit']=='1a1129ad3b2dc512a3951aca9cdada97d966e774'
        unit=units[expected['unit_id']];idx=key(expected['candidate'])
        assert unit['root_id']==cp['root_id'] and unit['candidate']==expected['candidate']
        def cost(occ,cfg):
            identity=dict(cp,exact_sql_id=occ['exact_sql_id'],configuration_id=cfg)
            return store[tuple(identity[f] for f in FIELDS)]
        occurrences=cp['occurrences'];workload=[o['sql'] for o in occurrences]
        roots=[cost(o,cp['root_id']) for o in occurrences]
        total=0.
        for value in roots:total+=value
        targets={o['sql']:cost(o,unit['add_id']) for o in occurrences}
        assert len(targets)==len({o['exact_sql_id'] for o in occurrences})
        masks=[{key(v) for v in x} for x in cp['native_query_indexes_per_occurrence']]
        old={(t,tuple(cs)) for t,cs in cp['native_root']}
        state=cp['native_observation']['pre_decision_state']
        state={name:{key(k) if '(' in k and k.endswith(')') else k:v for k,v in value.items()} if isinstance(value,dict) else value for name,value in state.items()}
        b=Backend(workload,roots,targets,old,cap=len(workload));t=tuner(b,state)
        rb=Backend(workload,roots,targets,old,cap=len(workload));r=tuner(rb,state,reference)
        # Only the reference retains the historical mode switch; Core has one rule.
        r.width2_add_full_window=True
        status=t._test_candidate(idx,masks,roots,total,old,workload)
        r._test_candidate(idx,masks,roots,total,old,workload)
        value=t._last_obs_delta_map[idx];wanted=float(expected['G_full' if len(idx[1])==2 else 'G_mask'])
        assert status=='COMPLETE' and math.isclose(value,wanted,abs_tol=1e-8,rel_tol=1e-10),(expected['unit_id'],value,wanted)
        assert b.calls==rb.calls and history(t)==history(r),(expected['unit_id'],'reference state/call mismatch')
        assert b.target==old and b.qcp_budget.used==len(b.calls)
        widths[len(idx[1])]+=1;recovered+=int(len(idx[1])==2 and float(expected['G_mask'])<=0<value)
        max_error=max(max_error,abs(value-wanted))
        rows.append(dict(unit_id=expected['unit_id'],workload=expected['workload'],window=expected['window'],candidate=expected['candidate'],
                         status=status,scope=t._last_candidate_observation['scope'],raw_delta=value,reference_delta=wanted,
                         query_mask_hits=sum(idx in q for q in masks),measured_occurrences=len(b.calls),qcp=b.qcp_budget.used))
    assert widths=={1:253,2:237} and recovered==73 and len(rows)==490
    with (a.output/'core_observations.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    report=dict(status='PASS',actual_core_method_calls=490,actual_reference_m2q_method_calls=490,
                width1_legacy=253,width2_full=237,recovered_targeted_positive=73,
                reference_state_and_calls_equal=490,max_abs_raw_error=max_error,
                atol=1e-8,rtol=1e-10,fixture_hashes=hashes,
                coverage_boundary='490 fixed captured states only; no fabricated new-candidate/configuration response or 603-round closed-loop equivalence claim',live_cache_preloaded=False)
    (a.output/'regression_report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='fixture_hashes'},indent=2))

if __name__=='__main__':main()
