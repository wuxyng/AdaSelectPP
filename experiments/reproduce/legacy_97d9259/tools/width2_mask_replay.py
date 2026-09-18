"""Offline replay through the patched _test_candidate, using exact per-query evidence."""
import argparse
import ast
import copy
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from adasel.ada_select import AdaSelect
from test_width2_mask_fix import Backend, tuner

BASE = '1a1129ad3b2dc512a3951aca9cdada97d966e774'
FIELDS = ('cohort','run','epoch','root_id','context_id','estimator','protocol','exact_sql_id','configuration_id')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def key(text):
    table, columns = text[:-1].split('(', 1)
    return table, tuple(columns.split(','))


def restore(state):
    result = copy.deepcopy(state)
    for name, value in result.items():
        if isinstance(value, dict):
            result[name] = {key(k) if '(' in k and k.endswith(')') else k: v for k,v in value.items()}
    return result


def history(t):
    return copy.deepcopy({k:v for k,v in t.__dict__.items() if k.startswith('idx_') or k in
                         ('columns_benefit','_last_obs_delta_map','_last_obs_src_map','_m_stats')})


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--diagnostic',type=Path,required=True)
    ap.add_argument('--package',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    contract=read(args.package/'EXPERIMENT_CONTRACT.json')
    manifest_path=args.diagnostic/'run_manifest.json'
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest()==contract['source_manifest_sha256']
    source=subprocess.check_output(['git','show',BASE+':adasel/ada_select.py'],cwd=ROOT).decode()
    namespace={};exec(compile(source,'verified_original_ada_select.py','exec'),namespace)
    original=namespace['AdaSelect']
    store={}
    for line in (args.diagnostic/'diagnostic_evidence.jsonl').read_text().splitlines():
        row=json.loads(line)
        if row['quality']!='OK':continue
        k=tuple(row['key'][f] for f in FIELDS)
        assert math.isfinite(row['cost']) and row['cost']>=0
        assert k not in store or store[k]==row['cost']
        store[k]=row['cost']
    plan=read(args.diagnostic/'sample_plan.json')
    units={u['unit_id']:u for u in plan['selected']}
    refs=list(csv.DictReader((args.package/'reference/expected_observations.csv').open(encoding='utf-8-sig')))
    outputs=[]; width_counts={1:0,2:0};recovered=0;max_error=0.;source_hashes={}
    for ref in refs:
        rel=Path('measure')/ref['workload']/'checkpoints'/(ref['window']+'.json.gz')
        path=args.diagnostic/rel
        cp=json.load(gzip.open(path,'rt',encoding='utf-8'))
        assert cp['checkpoint_id']==ref['checkpoint_id'] and cp['root_id']==ref['root_id'] and cp['epoch']==ref['epoch']
        assert cp['native_baseline_commit']==BASE
        source_hashes[str(rel)]=hashlib.sha256(path.read_bytes()).hexdigest()
        unit=units[ref['unit_id']]; idx=key(ref['candidate'])
        assert unit['root_id']==cp['root_id'] and unit['candidate']==ref['candidate']
        def cost(occ,cfg):
            identity=dict(cp,exact_sql_id=occ['exact_sql_id'],configuration_id=cfg)
            return store[tuple(identity[f] for f in FIELDS)]
        occurrences=cp['occurrences'];workload=[o['sql'] for o in occurrences]
        roots=[cost(o,cp['root_id']) for o in occurrences]
        # The registered workstation uses pre-3.12 left-to-right float sum.
        # Python 3.12's compensated built-in sum changes cancellation at zero.
        base_total=0.
        for value in roots:base_total+=value
        targets={o['sql']:cost(o,unit['add_id']) for o in occurrences}
        assert len(targets)==len({o['exact_sql_id'] for o in occurrences})
        masks=[{key(v) for v in row} for row in cp['native_query_indexes_per_occurrence']]
        old={(t,tuple(cs)) for t,cs in cp['native_root']}
        state=restore(cp['native_observation']['pre_decision_state'])
        legacy_backend=Backend(workload,roots,targets,old,cap=len(workload))
        legacy=tuner(legacy_backend,False,state,original)
        legacy._test_candidate(idx,masks,roots,base_total,old,workload)
        for mode in ('legacy','fixed'):
            backend=Backend(workload,roots,targets,old,cap=len(workload))
            t=tuner(backend,mode=='fixed',state)
            t.width2_add_full_window=mode=='fixed'
            status=t._test_candidate(idx,masks,roots,base_total,old,workload)
            actual=t._last_obs_delta_map.get(idx)
            expected=float(ref['G_full' if mode=='fixed' and len(idx[1])==2 else 'G_mask'])
            assert status=='COMPLETE' and math.isclose(actual,expected,abs_tol=1e-8,rel_tol=1e-10), (ref['unit_id'],mode,status,actual,expected)
            max_error=max(max_error,abs(actual-expected))
            if mode=='legacy':
                assert backend.calls==legacy_backend.calls
                assert history(t)==history(legacy), ref['unit_id']
            else:
                width_counts[len(idx[1])]+=1
                recovered+=int(len(idx[1])==2 and float(ref['G_mask'])<=0<actual)
            assert backend.target==old
            outputs.append(dict(unit_id=ref['unit_id'],mode=mode,status=status,raw_delta=actual,
                                query_mask_hits=sum(idx in q for q in masks),measured_occurrences=len(backend.calls),qcp=backend.qcp_budget.used))
    assert width_counts=={1:253,2:237} and recovered==73
    # Replay the actual native call order from all 603 capture checkpoints. No new scheduler.
    controls=[]
    for case in contract['cases']:
        for r,cap in enumerate(case['qcp_budget_per_window']):
            path=args.diagnostic/'capture'/case['name']/'checkpoints'/f'{r}.json.gz'
            cp=json.load(gzip.open(path,'rt',encoding='utf-8'))
            used=len(cp['occurrences']); sequence=[['root',used]]
            for e in cp['native_observation']['events']:
                if e['stage'] in ('ordinary_measurement','replacement_measurement'):
                    required=len(e['mask_indices'])
                    # A structural diagnostic with no prefix cannot measure anything.
                    if e['stage']=='replacement_measurement' and e.get('prefix') is None: required=0
                    assert required<=cap-used,(case['name'],r,e['candidate'],required,cap-used)
                    used+=required;sequence.append([e['stage'],e['candidate'],required])
            assert used==cp['policy_qcp_attempts']==cap,(case['name'],r,used,cap)
            controls.append(dict(workload=case['name'],window=r,cap=cap,used=used,sequence=sequence))
    assert len(controls)==603 and sum(c['used'] for c in controls)==22271
    with (args.output/'patched_observations.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(outputs[0]));w.writeheader();w.writerows(outputs)
    (args.output/'original_control_budget_replay.json').write_text(json.dumps(controls,indent=2))
    report=dict(status='PASS',actual_test_candidate_calls=1470,patched_observations=len(outputs),
                width1_legacy=253,width2_full=237,recovered_targeted_positive=73,
                disabled_vs_original_state_and_call_sequence_equal=490,
                native_control_windows_fit=603,native_control_qcp=22271,max_abs_raw_error=max_error,
                diagnostic_fixture_sha256=source_hashes,live_cache_preloaded=False)
    (args.output/'regression_report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='diagnostic_fixture_sha256'},indent=2))


if __name__=='__main__':main()
