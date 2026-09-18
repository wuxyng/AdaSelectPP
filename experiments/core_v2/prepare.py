"""Offline registration: verify profile ancestry and freeze explicit resources, no SQL."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from adasel.policy import POLICY_DEFAULTS


def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def verify_profiles(profiles,previous):
    assert previous['release']['commit']=='97d925989e53bdeea282be3b7ec66ef6c221f251'
    old={c['name']:c for c in previous['contract']['cases']}
    assert [c['name'] for c in profiles['cases']]==[c['name'] for c in previous['contract']['cases']]
    assert len(old)==9 and profiles['expected_runs']==9 and profiles['expected_windows']==603
    for c in profiles['cases']:
        r=old[c['name']]
        assert c['qcp_budget_per_window']==r['qcp_budget_per_window'],c['name']
        for k in ('max_num','alpha','beta'):assert c['policy_inputs'][k]==r['config'][k]
        assert set(c['policy_inputs'])=={'max_num','alpha','beta'}
        assert c['workload_sha256']==r['workload_sha256']
        assert c['workload_locator_hint']==r['workload']
        assert c['round_size']==r['round_size'] and c['rounds']==r['rounds']
        assert c['query_occurrences']==r['query_rows'] and c['query_timeout_ms']==r['config']['timeout']
        for k,v in c['constants_from_source'].items():
            assert v==r['config'][k]
            if k in POLICY_DEFAULTS:assert v==POLICY_DEFAULTS[k]
    assert sum(len(c['qcp_budget_per_window']) for c in profiles['cases'])==603
    assert sum(sum(c['qcp_budget_per_window']) for c in profiles['cases'])==22271


def main():
    p=argparse.ArgumentParser();p.add_argument('--previous',type=Path,required=True)
    p.add_argument('--diagnostic',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();profiles=read(ROOT/'experiments/core_v2/RELEASE_PROFILES.json');previous=read(a.previous)
    verify_profiles(profiles,previous)
    assert sha(a.diagnostic/'run_manifest.json')==previous['contract']['source_manifest_sha256']
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip()
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'FREEZE_REQUIRES_CLEAN_WORKTREE'
    tracked=subprocess.check_output(['git','ls-files'],cwd=ROOT).decode().splitlines()
    prefixes=('adasel/','adaselect_pp/candidate_gen_v2/','adaselect_pp/common/','tools/evaluation_substrate/',
              'experiments/core_v2/','txt/')
    singles={'database/__init__.py','database/safe_backend.py','database/core_backend.py','database/execution_errors.py','database/hypopg_action_scope.py',
             'util/__init__.py','util/benefit_normalizer.py','util/indexable_columns.py','adaselect_pp/__init__.py'}
    files={name:sha(ROOT/name) for name in tracked if name.startswith(prefixes) or name in singles}
    checkpoints={}
    for db in ('tpch','tpchs','job'):
        cp=next((a.diagnostic/'measure'/(db+'_shifting')/'checkpoints').glob('*.json.gz'))
        checkpoints[db]={'path':str(cp),'sha256':sha(cp)}
    a.output.mkdir(parents=True,exist_ok=False)
    manifest=dict(campaign_id='core_v2_'+head[:12],release=dict(commit=head,files=files),profiles=profiles,
                  source_reference='97d925989e53bdeea282be3b7ec66ef6c221f251',
                  profile_sha256=sha(ROOT/'experiments/core_v2/RELEASE_PROFILES.json'),
                  previous_registration_sha256=sha(a.previous),snapshot_checkpoints=checkpoints,
                  expected_indexes=previous['expected_indexes'],policy_cache='NONE; occurrence-paid; no preload; no carry',
                  constants=dict(POLICY_DEFAULTS),status='REGISTERED_NOT_STARTED',automatic_followup='STOP',
                  time_definition='exec=observed EXPLAIN ANALYZE Plan.Actual Total Time plus separately reported historical statement-timeout penalty; fatal failures stop and have no penalty; rec=inclusive run wall minus execution wall and transition wall; trans=owned DDL, reset and final cleanup; total=exec+rec+trans; unfinalized timing fields are known lower bounds, not complete measurements')
    (a.output/'run_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(commit=head,files=len(files),runs=9,windows=603,qcp_cap=22271,output=str(a.output))))

if __name__=='__main__':main()
