import ast
import copy
import itertools
import subprocess
from types import SimpleNamespace
import pytest

from adasel.ada_select import AdaSelect
from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from support import Backend, tuner, reference_class, history, ROOT, REFERENCE


@pytest.fixture(scope='module')
def reference():return reference_class()


@pytest.mark.parametrize('first',[True,False])
@pytest.mark.parametrize('beta',[.8,1.,1.1,1.5])
@pytest.mark.parametrize('values',[(0,0),(-2,-1),(-1,2),(2,-1),(1,1),(1,1.2),(0,1),(1,0)])
def test_selector_sign_zero_beta_and_first_round_match(reference, first, beta, values):
    old=('t',('a',));new=('t',('b',))
    actual=tuner(Backend(['q'],[10]));expected=tuner(Backend(['q'],[10]),cls=reference)
    for t in [actual,expected]:
        t.max_num=1;t.workload_count=0 if first else 2;t.beta=beta
        t.columns_benefit={old:values[0],new:values[1]}
        # Exercise all selector proxy sign branches independently of normalization.
        t._log_positive_norm=lambda data:dict(data)
        t._creation_cost=lambda k:0.
    assert actual._choose_config({old})==expected._choose_config({old})
    assert actual._last_net_benefit_map==expected._last_net_benefit_map
    assert actual._last_decision_stats==expected._last_decision_stats


@pytest.mark.parametrize('values',[[0,0,0],[-1,2,5],[1,1,1],[1e12,100,1],[-2,-3,-4]])
@pytest.mark.parametrize('capacity',[0,1,2,10])
def test_real_normalization_creation_filter_stable_ties(reference,values,capacity):
    keys=[('t',(c,)) for c in 'abc']
    a=tuner(Backend(['q'],[10]));b=tuner(Backend(['q'],[10]),cls=reference)
    for t in [a,b]:
        t.columns_benefit=dict(zip(keys,values));t.max_num=capacity;t.workload_count=0
        t.benefit_norm=SimpleNamespace(index_costs={(c,):v for c,v in zip('abc',[0.,.6,1.])},index_costs_by_key={})
    assert a._log_positive_norm(a.columns_benefit)==b._log_positive_norm(b.columns_benefit)
    assert a._choose_config({keys[0]})==b._choose_config({keys[0]})
    assert a._last_candidate_conf==b._last_candidate_conf


@pytest.mark.parametrize('alpha',[.35,.5,.55,.6,.7,.8,.9])
def test_adaptive_ewma_tracking_gated_updates_match(reference,alpha):
    a=tuner(Backend(['q'],[10]));b=tuner(Backend(['q'],[10]),cls=reference)
    idx=('t',('a',))
    for t in [a,b]:t.alpha_init=alpha
    for delta,src,hits,ok in [(10,'OK',1,1),(-3,'OK',1,1),(20,'OK',3,3),(0,'NO_HIT',0,0),(2,'PARTIAL_FALLBACK',3,1),(0,'ALL_FALLBACK',3,0),(-7,'OK',2,2)]:
        p=a.columns_benefit.get(idx,0.)
        la=a._choose_lambda(idx,p,delta,obs_src=src,hit_cnt=hits,ok_cnt=ok)
        lb=b._choose_lambda(idx,p,delta,obs_src=src,hit_cnt=hits,ok_cnt=ok)[0]
        assert la==lb
        a.columns_benefit[idx]=b.columns_benefit[idx]=la*p+(1-la)*delta
        assert history(a)==history(b)


def test_structural_eligibility_ranking_and_ordinary_ties_match(reference):
    candidates=[('t',(str(i),'b')) for i in range(7)]
    families=['JOIN_RANGE','EQ_RANGE','JOIN_EQ','EQ_EQ','RANGE1','', 'EQ_EQ']
    meta={k:dict(family=f,score=i,seed_normalized_benefit=i%2) for i,(k,f) in enumerate(zip(candidates,families))}
    a=tuner(Backend(['q'],[10]));b=tuner(Backend(['q'],[10]),cls=reference)
    a.batch=SimpleNamespace(stage='grow',metadata=meta,scores={})
    b._candidate_meta_map=lambda:meta;b._last_wdcg_score_map={}
    old={candidates[6]}
    eligible=[]
    for k in candidates:
        assert a._is_structural_pair_candidate(k,old)==b._is_structural_pair_candidate(k,old)
        if a._is_structural_pair_candidate(k,old):eligible.append(k)
    assert a._rank_structural_pair_candidates(eligible)==b._rank_structural_pair_candidates(eligible)
    assert a._ordered_candidates(set(candidates),old)[0]==b._rank_structural_pair_candidates(eligible)[0]


class Schema:
    cols={'t':list('abcdef'),'u':list('abcxyz')}
    def get_tables(self):return list(self.cols)
    def get_columns(self,t):return self.cols[t]
    def exec_fetchall(self,sql):return []


@pytest.fixture(scope='module')
def original_generator():
    text=subprocess.check_output(['git','show',REFERENCE+':adaselect_pp/candidate_gen_v2/generator.py'],cwd=ROOT).decode()
    ns={'__package__':'adaselect_pp.candidate_gen_v2'}
    exec(compile(text,'verified_original_generator.py','exec'),ns)
    # Original output container is isolated from the new explicit stage container.
    ns['GenerationResult']=lambda q,k,s,m,stats:SimpleNamespace(query_indexes=q,topk_set=k,score_map=s,meta_map=m,stats=stats)
    return ns['MCIGCandidateGenerator']


SQLS=[
    'select * from t where a=1 and b=2 and c>3',
    'select * from t join u on t.a=u.a where t.b>5 and u.x=1',
    'select * from t where a=1 or b=2',
    'select * from t where a>1 and b>2',
    'select * from t where a=1 and b=2 and c=3 and d=4 and e=5 and f>6',
    'select * from t join u on t.a=u.a where t.b=2 and t.c>3 and u.b=4 and u.c>5',
]


@pytest.mark.parametrize('sql',SQLS)
@pytest.mark.parametrize('stage_round',[0,1,2,8])
@pytest.mark.parametrize('maturity',['unseen','positive','negative','fallback'])
def test_generator_candidates_membership_scores_and_native_metadata_match(tmp_path,original_generator,sql,stage_round,maturity):
    path=tmp_path/'columns.txt'
    path.write_text('\n'.join(t+' '+c for t,cols in Schema.cols.items() for c in cols))
    args=dict(indexable_path=str(path),max_num=46,per_query_cap=12,per_table_cap=4,round_table_cap=6)
    a=MCIGCandidateGenerator('fake',Schema(),**args);b=original_generator('fake',Schema(),**args)
    seeds={(t,(c,)) for t,cols in Schema.cols.items() for c in cols}
    state=dict(seed_benefit={k:(-1 if maturity=='negative' else 1) for k in seeds},
               seed_seen_count={k:(0 if maturity=='unseen' else 1) for k in seeds},
               seed_positive_count={k:1 for k in seeds},
               seed_last_obs_src={k:('ALL_FALLBACK' if maturity=='fallback' else 'OK') for k in seeds})
    x=a.generate([sql,sql],workload_count=stage_round,topk=46,**state)
    y=b.generate([sql,sql],workload_count=stage_round,topk=46,candidate_generation_mode='probe_grow_fair',**state)
    assert x.stage==y.stats['gen_mode']
    assert x.query_indexes==y.query_indexes and x.topk_set==y.topk_set and x.score_map==y.score_map
    for k in x.topk_set:
        for field in ('family','score','seed_key','grow_reason','seed_normalized_benefit','expected_structural_pair_type'):
            assert x.meta_map[k].get(field)==y.meta_map[k].get(field),(k,field)
    assert len(x.topk_set)<=46 and all(len(k[1])<=2 for k in x.topk_set)


def test_core_imports_without_research_or_learned_dependencies():
    code='''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.startswith(('torch','tcnn','adaselect_pp.action_','adaselect_pp.legacy_budget','model1','mcts')):
   raise AssertionError('forbidden import '+fullname)
sys.meta_path.insert(0,Block())
import adasel.ada_select, database.core_backend
assert not any(x.startswith(('torch','tcnn','adaselect_pp.action_')) for x in sys.modules)
'''
    import sys
    subprocess.run([sys.executable,'-c',code],cwd=ROOT,check=True)


@pytest.mark.parametrize('limit,table_cap',[(1,1),(2,2),(3,2),(4,3),(8,6)])
@pytest.mark.parametrize('reverse_pairs',[False,True])
def test_fixed_fairness_caps_diversity_and_structural_rank_match(original_generator,limit,table_cap,reverse_pairs):
    from adaselect_pp.candidate_gen_v2.types import Candidate
    merged={}
    for table in ['t','u']:
        for i,cols in enumerate([('a',),('b',),('c',),('a','b'),('b','a') if reverse_pairs else ('a','c')]):
            key=(table,cols)
            c=Candidate(key=key,family='EQ_EQ' if len(cols)==2 else 'EQ1',source='AST',confidence=.9)
            c.score=100-i
            merged[key]=c
    meta={('t',('a','b')):{'expected_structural_pair_type':'JOIN_RANGE'}}
    a=MCIGCandidateGenerator.__new__(MCIGCandidateGenerator)
    b=original_generator.__new__(original_generator)
    for gen in [a,b]:
        gen.round_table_cap=table_cap
        gen.db=SimpleNamespace(exec_fetchall=lambda *_:pytest.fail('fairness issued SQL'))
    x=a._round_select(merged,limit,meta)
    y,_=b._round_select_with_diagnostics(merged,topk=limit,pair_supply_fairness_enabled=True,
            pair_supply_per_table_width2_reserve=1,pair_supply_round_width2_reserve=4,grow_meta=meta)
    assert [c.key for c in x]==[c.key for c in y]
    assert len(x)<=limit
    assert all(sum(c.key[0]==t for c in x)<=table_cap for t in ['t','u'])
    if limit==2 and table_cap==2:
        assert ('t',('a','b')) in {c.key for c in x}
        assert all(sum(c.key[0]==table and len(c.key[1])==2 for c in x)==1 for table in ['t','u'])
    if reverse_pairs:
        for t in ['t','u']:
            pairs=[frozenset(c.key[1]) for c in x if c.key[0]==t and len(c.key[1])==2]
            assert len(pairs)==len(set(pairs))
