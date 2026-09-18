import ast
import hashlib
import json
import time
from types import SimpleNamespace
import pytest
from database.core_backend import Meter,Backend
from database.safe_backend import CampaignStop
from adasel.measurement import QCPBudget
from support import ROOT


def test_accepted_transport_and_transaction_repair_are_unchanged():
    manifest=json.loads((ROOT/'docs/core_v2_safety_provenance.json').read_text())
    nodes={n.name:n for n in ast.parse((ROOT/'database/safe_backend.py').read_text()).body if hasattr(n,'name')}
    for name,expected in manifest.items():
        if name.startswith('adapter_ast:'):
            value=ast.dump(nodes[name.split(':',1)[1]],include_attributes=False).replace(', type_params=[]','').encode()
        else:value=(ROOT/name).read_bytes().replace(b'\r\n',b'\n')
        assert hashlib.sha256(value).hexdigest()==expected,name
    assert 'connection.autocommit = False' in ast.unparse(nodes['make_session'])


@pytest.mark.parametrize('purpose',['root','ADD','KEEP'])
def test_live_meter_charges_failure_and_refuses_before_next_attempt(purpose):
    meter=Meter(time.time()+30);meter.purpose=purpose
    backend=Backend(None,None);backend.session_meter=meter
    budget=QCPBudget(1);backend.bind_budget(budget)
    assert backend.qcp_budget is meter.budget is budget
    token=meter.begin()
    assert budget.used==1 and meter.attempts==1
    meter.end(token,time.monotonic(),RuntimeError('failed EXPLAIN'))
    assert meter.failures==1
    with pytest.raises(CampaignStop,match='QCP_ACCOUNTING_ERROR'):meter.begin()
    assert meter.attempts==budget.used==1


def test_release_profiles_exactly_preserve_registered_ancestry():
    # The external registration comparison is also executed by prepare.py before live.
    p=json.loads((ROOT/'experiments/core_v2/RELEASE_PROFILES.json').read_text())
    assert p['live_limits']==dict(campaign_wall_s=7200,cleanup_grace_s=900,separate_preflight_explain_cap=30)
    assert all(c['query_timeout_ms']==30000 for c in p['cases'])
    assert [c['name'] for c in p['cases']]==[f'{db}_{trace}' for db in ['tpch','tpchs','job'] for trace in ['shifting','noisy','random']]
