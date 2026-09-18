"""Adapted from the independent auditor's 2026-09-18 no-DB reproducer.
Original SHA256: 63578184dd672086df037cc026a55101bcdc270aeb01f9b925ca1fd0a5fdfc45.
Real main/run_one/AdaSelect/report; only transport/process boundaries are fake.
"""
import csv
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(os.environ.get('CORE_AUDIT_SOURCE', Path(__file__).resolve().parents[2]))
AUDIT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from adasel.main import main
from adasel.ada_select import AdaSelect
from database.safe_backend import CampaignStop, EvidenceError
from experiments.core_v2 import release, report

EPOCH = dict(database_environment={}, statistics_fingerprint={}, schema_fingerprint={}, epoch_hash='fake-stable-epoch')


class PgError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.pgcode = code


class Cursor:
    def __init__(self, connection):
        self.c = connection
        self.statement = ''
        self.payload = None

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def execute(self, statement, params=None):
        self.statement = statement
        s = self.c.s
        s.events.append(['sql', self.c.role, statement, params])
        if statement.startswith('EXPLAIN (ANALYZE, FORMAT JSON) '):
            s.clock += .5
            s.runtime_attempts += 1
            value = s.actions.pop(0)
            if isinstance(value, BaseException): raise value
            self.payload = value if isinstance(value, (list, str)) else [{'Plan': {'Actual Total Time': value}, 'Execution Time': 98765}]
        elif s.setup_error and self.c.role == 'runtime' and 'set_config' in statement:
            value, s.setup_error = s.setup_error, None
            raise value

    def fetchone(self): return (self.payload,)

    def fetchall(self):
        if "SELECT tablename" in self.statement: return [('lineitem',)]
        if "SELECT column_name" in self.statement: return [('l_orderkey',), ('l_partkey',), ('l_quantity',)]
        return []


class Connection:
    def __init__(self, state, role): self.s, self.role = state, role
    def cursor(self): return Cursor(self)
    def close(self):
        self.s.events.append(['close', self.role])
        if self.role == self.s.close_error: raise RuntimeError('injected close failure')


class Owned:
    def __init__(self, state):
        self.s = state
        self.ddl_ms = 0.
        self.current = set()
        self.meter = None

    def transition(self, target, purpose):
        self.s.events.append(['transition', purpose, sorted(target)])
        self.current = set(target)
        if self.s.transition_error:
            self.s.clock += .3
            self.ddl_ms += 300
            raise EvidenceError('injected transition failure')

    def cleanup(self):
        self.s.events.append(['owned_cleanup'])
        self.s.clock += .1
        if self.s.cleanup_error: raise EvidenceError('injected restoration failure')
        self.current.clear()
        return dict(exact_restoration=True)


class Session:
    def __init__(self, state, meter, relations):
        self.s, self.meter, self.relevant_relations = state, meter, relations
        self.physical = {}
        self.protected = set()

    def _catalog(self): return {}
    def _assert_clean(self): self.s.events.append(['assert_clean'])
    def _capture_epoch(self, _):
        if self.s.final_validation_error and ['owned_cleanup'] in self.s.events:
            raise EvidenceError('injected final validation failure')
        return EPOCH.copy()

    def _evaluate(self, query, configuration):
        token = self.meter.begin()
        error = None
        self.s.events.append(['measurement', self.meter.attempts, query.sql])
        try:
            if self.meter.attempts == self.s.measurement_fail_at:
                raise self.s.measurement_error or CampaignStop('INJECTED_UNRECOVERABLE_MEASUREMENT')
            return SimpleNamespace(optimizer_cost=10.)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.meter.end(token, time.monotonic() - .001, error)


def run_fixture(monkeypatch, tmp_path, actions, *, round_size=2, cleanup_error=False,
                close_error=None, measurement_fail_at=None, setup_error=None, sqls=None, final_validation_error=False, transition_error=False, connect_error=None, measurement_error=None):
    """Real main -> run_one -> execute_occurrence -> cursor -> AdaSelect.run/reset.

    Registration, DB connections/catalog/session and owned DDL are substituted.
    Policy constructor, generator, scheduler, runtime SQL validation and reporting
    are actual checked-in methods. Real creation/vocabulary files are copied fixtures.
    """
    s = SimpleNamespace(events=[], actions=list(actions), clock=0., runtime_attempts=0,
                        cleanup_error=cleanup_error, close_error=close_error,
                        measurement_fail_at=measurement_fail_at, setup_error=setup_error, final_validation_error=final_validation_error, transition_error=transition_error,measurement_error=measurement_error)
    trace = tmp_path / 'trace.sql'
    sqls = sqls or ['SELECT 1' for _ in actions]
    trace.write_text('\n'.join(sqls))
    rounds = (len(sqls) + round_size - 1) // round_size
    case = dict(name='tpch_shifting', benchmark='tpch', policy_inputs=dict(max_num=10, alpha=.5, beta=1.1),
                qcp_budget_per_window=[20] * rounds, workload_sha256='fixture', query_timeout_ms=30000,
                workload_locator_hint=str(trace), round_size=round_size, rounds=rounds, query_occurrences=len(sqls))
    manifest = dict(release=dict(commit='a2d70bfe36b5c45ca1c519c715f5e6051de6ca0e'),
                    profiles=dict(cases=[case]), time_definition='fixture', automatic_followup='STOP')
    release.write_json(tmp_path/'run_manifest.json', manifest)
    owner = Owned(s)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(release, 'load', lambda _: manifest)
    def connection(m, db, role):
        if role == connect_error:raise ConnectionError('injected connect failure')
        return Connection(s, role)
    monkeypatch.setattr(release, 'connect', connection)
    monkeypatch.setattr(release, 'schema', lambda _: {'lineitem': ['l_orderkey', 'l_partkey', 'l_quantity']})
    monkeypatch.setattr(release, 'OwnedRoot', lambda *a: owner)
    monkeypatch.setattr(release, 'make_session', lambda c, relations, meter: Session(s, meter, relations))
    monkeypatch.setattr(release, 'reference_epoch', lambda *a: EPOCH.copy())
    monkeypatch.setattr(release, 'time', SimpleNamespace(monotonic=lambda: s.clock, time=time.time))
    original_run = AdaSelect.run
    def observed_run(self, *a, **kw):
        s.events.append(['tuner_run', kw['window_id']])
        return original_run(self, *a, **kw)
    monkeypatch.setattr(AdaSelect, 'run', observed_run)
    old_handler = signal.getsignal(signal.SIGTERM)
    failure = None
    try:
        main(['--output', str(tmp_path), '--deadline', str(time.time()+600), 'run', case['name']])
    except BaseException as exc:
        failure = dict(type=type(exc).__name__, error=str(exc))
    finally:
        signal.signal(signal.SIGTERM, old_handler)
    path = tmp_path/'runs'/case['name']
    saved = release.read(path/'run_manifest.json') if (path/'run_manifest.json').exists() else {}
    events = release.records(path/'error_events.jsonl') if hasattr(release, 'records') else [json.loads(x) for x in (path/'error_events.jsonl').read_text().splitlines()] if (path/'error_events.jsonl').exists() else []
    result = dict(failure=failure, runtime_attempts=s.runtime_attempts, events=s.events, error_events=events, manifest=saved)
    try: result['report'] = report.summarize(tmp_path)
    except Exception as exc: result['report_error'] = dict(type=type(exc).__name__, error=str(exc))
    (tmp_path/'audit_observation.json').write_text(json.dumps(result, indent=2))
    assert 'psycopg2' not in sys.modules, 'No live database driver may be imported'
    return result


def names(r): return [e[0] for e in r['events']]
def purposes(r): return [e[1] for e in r['events'] if e[0]=='transition']


def campaign_fixture(monkeypatch,tmp_path,mode):
    events=[]
    m=dict(profiles=dict(cases=[dict(name='tpch_shifting',qcp_budget_per_window=[20]),dict(name='job_shifting',qcp_budget_per_window=[20])],
                        live_limits=dict(campaign_wall_s=60,cleanup_grace_s=3)))
    release.write_json(tmp_path/'run_manifest.json',m)
    monkeypatch.setattr(release,'load',lambda _:m)
    class Worker:
        pid=123456
        def __init__(self,cmd,**kw):
            self.returncode=None
            self.job=cmd[-2:] if cmd[-2]=='run' else ['preflight']
            events.append(['spawn',self.job]);self.waits=0
        def terminate(self):events.append(['terminate'])
        def kill(self):events.append(['kill'])
        def wait(self,timeout=None):
            self.waits+=1;events.append(['wait',self.job,timeout])
            if self.job==['preflight']: return 0
            if mode=='parent_interrupt' and self.waits==1: raise KeyboardInterrupt('parent interrupted')
            path=tmp_path/'runs'/'tpch_shifting'/'run_manifest.json'
            if not path.exists():
                # Simulates last on-disk RUNNING receipt before an unpersisted paid attempt.
                r=dict(status='RUNNING') if mode in ('hard_kill','parent_interrupt') else dict(status='INCOMPLETE',rounds=0,qcp_used=7)
                release.write_json(path,r)
            if mode=='hard_kill' and self.waits<=2: raise release.subprocess.TimeoutExpired('fake',timeout)
            return 1
    monkeypatch.setattr(release.subprocess,'Popen',Worker)
    monkeypatch.setattr(release.os,'killpg',lambda pid,sig:events.append(['killpg',pid,str(sig)]),raising=False)
    monkeypatch.setattr(release.signal,'SIGKILL',9,raising=False)
    def recover(*a):
        events.append(['recover'])
        if mode=='recovery_failure': raise EvidenceError('recovery failed')
    monkeypatch.setattr(release,'recover_owned',recover)
    failure=None
    try: main(['--output',str(tmp_path),'campaign'])
    except BaseException as exc: failure=dict(type=type(exc).__name__,error=str(exc))
    r=dict(events=events,failure=failure,manifest=release.read(tmp_path/'run_manifest.json'))
    (tmp_path/'audit_supervisor.json').write_text(json.dumps(r,indent=2))
    return r
