import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from adasel.main import reject_policy_environment
from adasel.policy import PolicyConfig,POLICY_DEFAULTS
from experiments.core_v2.migrate_config import migrate
from support import ROOT


@pytest.mark.parametrize('key',['OPTIMIZER_RATIO','RATIO','OPRATIO','ADASEL_OPTIMIZER_RATIO','ADASELECT_RATIO','ADASEL_LAMBDA_POLICY'])
def test_retired_environment_input_explicitly_rejected(key):
    with pytest.raises(ValueError):reject_policy_environment({key:'0.5'})


@pytest.mark.parametrize('flag',['--optimizer_ratio','--optimizer-ratio','--opratio','--ratio','--max-evaluated-candidates'])
def test_retired_cli_input_rejected_before_driver(flag):
    r=subprocess.run([sys.executable,'-m','adasel.main','--output','unused',flag,'.5','campaign'],cwd=ROOT,capture_output=True,text=True)
    assert r.returncode==2 and 'removed policy input: '+flag in r.stderr


def test_offline_migration_requires_explicit_budget_not_fraction():
    old=dict(max_num=10,alpha=.5,beta=1.1,optimizer_ratio=.25)
    with pytest.raises(ValueError):migrate(old,[])
    with pytest.raises(ValueError):migrate(old,[.25])
    new=migrate(old,[2,10])
    assert new['qcp_budget_per_window']==[2,10]
    assert 'optimizer_ratio' not in new['policy_inputs']
    assert new['historical_removed_inputs']['optimizer_ratio']==.25
    assert PolicyConfig.from_mapping(new['policy_inputs']).qcp_budget==2
    with pytest.raises(ValueError):migrate(dict(old,replacement_overlay_enabled=True),[10])


def test_only_four_user_policy_keys_and_fixed_width_supply():
    assert set(PolicyConfig.__dataclass_fields__)=={'max_num','qcp_budget','alpha','beta'}
    assert POLICY_DEFAULTS['min_width']==1 and POLICY_DEFAULTS['max_width']==2
    assert POLICY_DEFAULTS['candidate_topk_factor']==4 and POLICY_DEFAULTS['candidate_topk_min_extra']==6
    assert POLICY_DEFAULTS['pair_supply_per_table_width2_reserve']==1
    assert POLICY_DEFAULTS['pair_supply_round_width2_reserve']==4
    assert POLICY_DEFAULTS['probe_rounds']==2


def test_active_profiles_have_603_explicit_budgets_and_no_retired_input():
    path=ROOT/'experiments/core_v2/RELEASE_PROFILES.json';p=json.loads(path.read_text())
    assert len(p['cases'])==9
    assert sum(len(c['qcp_budget_per_window']) for c in p['cases'])==603
    assert sum(sum(c['qcp_budget_per_window']) for c in p['cases'])==22271
    for c in p['cases']:
        assert set(c['policy_inputs'])=={'max_num','alpha','beta'}
        assert all(type(b) is int and b>=0 for b in c['qcp_budget_per_window'])
        assert 'optimizer_ratio' not in c['policy_inputs']


def test_actual_core_has_no_candidate_count_allowance_or_paid_diagnostic():
    from adasel.ada_select import AdaSelect
    import inspect
    source=inspect.getsource(AdaSelect)
    for token in ('optimizer_ratio','self.ratio','main_budget','max_evaluated_candidates','_record_structural_pair_replacement_diagnostic','_run_fairness_eval_lane','_last_wdcg_stats'):
        assert token not in source
    assert 'for idx in self._last_eval_order:' in source
