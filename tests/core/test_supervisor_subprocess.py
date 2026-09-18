"""Real OS children; no SQL, database driver or recovery connection is permitted."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from audit_execution_support import release


@pytest.mark.parametrize('mode',['interrupt','hard_kill','missing_receipt'])
def test_real_child_reaped_before_recovery_and_no_next_job(monkeypatch,tmp_path,mode):
    manifest=dict(profiles=dict(cases=[dict(name='tpch_shifting',qcp_budget_per_window=[20]),
                                     dict(name='job_shifting',qcp_budget_per_window=[20])],
                               live_limits=dict(campaign_wall_s=60,cleanup_grace_s=.3)))
    release.write_json(tmp_path/'run_manifest.json',manifest)
    monkeypatch.setattr(release,'load',lambda _:manifest)
    actual_popen=subprocess.Popen;children=[];events=[]
    # POSIX child deliberately ignores TERM in the hard-kill case. Windows has
    # TerminateProcess; that path is tested without pretending it is SIGKILL.
    child_code="""import pathlib,signal,sys,time
if sys.argv[2]=='hard_kill' and sys.platform!='win32':signal.signal(signal.SIGTERM,signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text('ready')
time.sleep(60)
"""
    class Worker:
        def __init__(self,cmd,**kwargs):
            self.preflight=cmd[-1]=='preflight';self.waits=0
            ready=tmp_path/('preflight_ready' if self.preflight else 'run_ready')
            code='pass' if self.preflight else child_code
            self.process=actual_popen([sys.executable,'-B','-c',code,str(ready),mode],**kwargs)
            self.pid=self.process.pid;children.append(self.process);events.append('spawn')
            if not self.preflight:
                limit=time.monotonic()+5
                while not ready.exists() and time.monotonic()<limit:time.sleep(.01)
                assert ready.exists()
        def wait(self,timeout=None):
            self.waits+=1;events.append('wait')
            if not self.preflight and self.waits==1:
                if mode=='interrupt':raise KeyboardInterrupt('controlled parent interrupt')
                raise subprocess.TimeoutExpired('controlled deadline',timeout)
            code=self.process.wait(timeout=timeout)
            events.append('reaped');return code
        def terminate(self):events.append('terminate');self.process.terminate()
        def kill(self):events.append('kill');self.process.kill()
    def recover(*_):
        assert all(child.poll() is not None for child in children)
        assert events[-1]=='reaped'
        events.append('recover')
        return dict(status='NO_DATABASE_FIXTURE')
    monkeypatch.setattr(release.subprocess,'Popen',Worker)
    monkeypatch.setattr(release,'recover_owned',recover)
    try:
        with pytest.raises((KeyboardInterrupt,subprocess.TimeoutExpired)):
            release.campaign(tmp_path)
        assert len(children)==2 and 'recover' in events
        assert all(child.poll() is not None for child in children)
        receipt=release.read(tmp_path/'run_manifest.json')
        if os.name!='nt' and mode=='hard_kill':assert children[-1].returncode==-signal.SIGKILL
        assert receipt['status']=='INCOMPLETE' and receipt['qcp_used'] is None
        assert receipt['qcp_actual_attempts'] is None and receipt['qcp_budget_charge']==20
        assert not receipt['qcp_counters_complete']
        assert receipt['started_jobs'][-1]['state']=='EXITED'
        assert 'psycopg2' not in sys.modules
        (tmp_path/'subprocess_evidence.json').write_text(json.dumps(dict(platform=sys.platform,mode=mode,events=events,pids=[c.pid for c in children],exit_codes=[c.returncode for c in children],receipt=receipt),indent=2))
    finally:
        for child in children:
            if child.poll() is None:child.kill()
            child.wait(timeout=5)


def test_inflight_qcp_is_unknown_and_charged_conservatively(tmp_path):
    from experiments.core_v2.receipts import ReceiptMeter,qcp_receipt
    from adasel.measurement import QCPBudget
    meter=ReceiptMeter(time.time()+60,tmp_path/'qcp.json');meter.budget=QCPBudget(5)
    token=meter.begin()  # simulate interruption before dispatch/response
    receipt=qcp_receipt(dict(status='RUNNING'),5,release.read(tmp_path/'qcp.json'))
    assert receipt['qcp_actual_attempts'] is None and receipt['qcp_used'] is None
    assert receipt['qcp_budget_charge']==5 and receipt['qcp_persisted_charge']==1
    assert receipt['qcp_inflight']['id']==token['id'] and not receipt['qcp_counters_complete']
