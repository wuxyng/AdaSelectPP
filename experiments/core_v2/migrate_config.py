"""Offline migration only: preserve ancestry without deriving any budget from it."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from adasel.policy import POLICY_DEFAULTS,PolicyConfig,nonnegative_integer


def migrate(old, budgets):
    if not budgets:raise ValueError('explicit QCP budget or registered vector required')
    for b in budgets:nonnegative_integer(b,'qcp_budget')
    fixed=dict(POLICY_DEFAULTS, candidate_generation_mode='probe_grow_fair',width2_add_full_window=True,
               wdcg_enabled=True,pair_supply_fairness_enabled=True,lambda_policy='adaptive',transition_mode='symmetric',
               replacement_overlay_enabled=False,fairness_eval_lane_enabled=False,pair_supply_ceiling_enabled=False)
    known=set(fixed)|{'max_num','alpha','beta','optimizer_ratio','ratio','timeout','indexable_columns_path',
                      'log_candidate_sample','fixed_lambda','benefit_decay','benefit_decay_fixed','fairness_eval_lane_quota','target_pair_audit'}
    if set(old)-known:raise ValueError('unknown old keys: '+str(sorted(set(old)-known)))
    for k,wanted in fixed.items():
        if k in old and old[k]!=wanted:raise ValueError('unsupported old behavior: '+k)
    if old.get('benefit_decay') is not None or old.get('target_pair_audit'):
        raise ValueError('unsupported explicit decay/target audit behavior')
    policy=PolicyConfig.from_mapping({**{k:old[k] for k in ('max_num','alpha','beta')},'qcp_budget':budgets[0]})
    return dict(policy_inputs=policy.as_dict(),qcp_budget_per_window=budgets,
                historical_removed_inputs={k:old[k] for k in ('optimizer_ratio','ratio') if k in old},
                removed_keys=sorted(set(old)-{'max_num','alpha','beta'}),
                notice='Historical values never set budgets or candidate evaluation allowances.')


def main():
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('--output',type=Path,required=True)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--qcp-budget',type=int);g.add_argument('--budget-file',type=Path)
    a=p.parse_args();old=json.loads(a.source.read_text(encoding='utf-8'))
    budgets=json.loads(a.budget_file.read_text()) if a.budget_file else [a.qcp_budget]
    result=migrate(old,budgets);a.output.write_text(json.dumps(result,indent=2),encoding='utf-8')

if __name__=='__main__':main()
