"""Registered safe HypoPG transport, transaction repair and owned DDL.

Safety methods are extracted unchanged from the accepted mask diagnostic adapter.
Only the obsolete action-config convenience method is omitted; Backend below
uses the exact configuration/evaluation path already used by 97d9259 M2Q.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from tools.evaluation_substrate.schema import ConfigurationSpec, IndexDefinition, QuerySpec
from adaselect_pp.common import sql_only
from adasel.measurement import QCPBudget

def stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(stable(value).encode()).hexdigest()

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')

class EvidenceError(ValueError):
    pass

class CampaignStop(BaseException):
    pass

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def append(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(stable(row) + '\n')
        f.flush()
        os.fsync(f.fileno())

def records(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines() if s] if Path(path).exists() else []

def connect(manifest, db, role):
    import psycopg2
    if db not in ('tpch', 'tpchs', 'job'):
        raise EvidenceError('DATABASE_NOT_ALLOWLISTED')
    c = psycopg2.connect(host='localhost', port=50222, user='lx', dbname=db, application_name=manifest['campaign_id'] + ':' + role, connect_timeout=10)
    c.autocommit = True
    with c.cursor() as q:
        q.execute('SELECT set_config(%s,%s,false)', ('statement_timeout', '10000'))
    return c

def sqlrows(c, statement, params=None):
    with c.cursor() as q:
        q.execute(statement, params)
        return q.fetchall()

def catalog(c):
    return sqlrows(c, "SELECT i.indexrelid,ct.relname,ci.relname,pg_get_indexdef(i.indexrelid),i.indisunique,i.indisprimary,i.indisvalid,i.indisready FROM pg_index i JOIN pg_class ct ON ct.oid=i.indrelid JOIN pg_class ci ON ci.oid=i.indexrelid JOIN pg_namespace n ON n.oid=ct.relnamespace WHERE n.nspname='public' ORDER BY ct.relname,ci.relname")

def qid(x):
    return '"' + str(x).replace('"', '""') + '"'

class OwnedRoot:
    """DDL only for allowlisted source roots/native decisions; never diagnostic targets."""

    def __init__(self, c, manifest, db, out, allowed):
        self.c, self.m, self.db, self.out = (c, manifest, db, Path(out))
        self.allowed = {tuple((t, tuple(cols))) for t, cols in allowed}
        self.before = catalog(c)
        self.owned = {}
        self.current = set()
        self.ddl_ms = 0.0
        self.events = []
        self.meter = None
        expected = {(r['table'], r['name'], r['definition']) for r in manifest['expected_indexes'][db]}
        if {(r[1], r[2], r[3]) for r in self.before} != expected:
            raise EvidenceError('INITIAL_CATALOG_NOT_BASELINE')
        if not all((r[4] or r[5] for r in self.before)):
            raise EvidenceError('UNMANAGED_UNPROTECTED_INDEX')
        if not sqlrows(c, 'SELECT pg_try_advisory_lock(20260917,23017)')[0][0]:
            raise EvidenceError('EXPERIMENT_LEASE_BUSY')
        clients = sqlrows(c, "SELECT application_name FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid() AND application_name NOT LIKE %s", (manifest['campaign_id'] + '%',))
        if clients:
            raise EvidenceError('OTHER_DATABASE_CLIENTS_PRESENT')
        write_json(self.out / 'initial_catalog.json', self.before)

    def name(self, key):
        return 'mdv2_' + digest([self.m['campaign_id'], self.db, key])[:24]

    def _ddl(self, statement, purpose, key):
        cleanup = purpose == 'FINAL_SOURCE_RESTORATION'
        if self.meter and (not cleanup):
            self.meter.check()
        start = time.monotonic()
        append(self.out / 'ddl_events.jsonl', {'stage': 'before', 'purpose': purpose, 'index': key, 'statement': statement})
        try:
            with self.c.cursor() as q:
                timeout = 10000 if cleanup else min(30000, max(1, int((self.meter.deadline - time.monotonic()) * 1000))) if self.meter else 30000
                q.execute('SELECT set_config(%s,%s,false)', ('statement_timeout', str(timeout)))
                q.execute(statement)
        finally:
            elapsed = (time.monotonic() - start) * 1000
            self.ddl_ms += elapsed
            append(self.out / 'ddl_events.jsonl', {'stage': 'after', 'purpose': purpose, 'index': key, 'elapsed_ms': elapsed})

    def transition(self, target, purpose):
        target = {(t, tuple(cs)) for t, cs in target}
        if not target <= self.allowed:
            raise EvidenceError('ROOT_OUTSIDE_REGISTERED_ALLOWLIST')
        for key in sorted(self.current - target):
            self._ddl('DROP INDEX public.' + qid(self.owned[key]), purpose, key)
            self.current.remove(key)
            self.owned.pop(key)
        for key in sorted(target - self.current):
            table, cols = key
            name = self.name(key)
            self.owned[key] = name
            self._ddl('CREATE INDEX ' + qid(name) + ' ON public.' + qid(table) + ' (' + ','.join((qid(c) for c in cols)) + ')', purpose, key)
            self.current.add(key)
        actual = catalog(self.c)
        if [r for r in actual if r[2] not in self.owned.values()] != self.before:
            raise EvidenceError('UNMANAGED_OR_PROTECTED_CATALOG_CHANGED')
        if {r[2] for r in actual if r[2] in self.owned.values()} != set(self.owned.values()):
            raise EvidenceError('ROOT_DDL_NOT_ACHIEVED')
        definition_rows = sqlrows(self.c, "SELECT ci.relname,ct.relname,ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum,n) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum ORDER BY k.n) FROM pg_index i JOIN pg_class ci ON ci.oid=i.indexrelid JOIN pg_class ct ON ct.oid=i.indrelid JOIN pg_namespace n ON n.oid=ct.relnamespace WHERE n.nspname='public'")
        observed = {name: (table, tuple(cols)) for name, table, cols in definition_rows if name in self.owned.values()}
        if observed != {name: key for key, name in self.owned.items()}:
            raise EvidenceError('PHYSICAL_SOURCE_DEFINITION_MISMATCH')

    def cleanup(self):
        errors = []
        for key, name in list(self.owned.items()):
            try:
                self._ddl('DROP INDEX IF EXISTS public.' + qid(name), 'FINAL_SOURCE_RESTORATION', key)
                self.owned.pop(key)
                self.current.discard(key)
            except BaseException as exc:
                errors.append(type(exc).__name__ + ':' + str(exc))
        after = catalog(self.c)
        if after != self.before:
            errors.append('EXACT_CATALOG_RESTORATION_FAILED')
        result = {'before': self.before, 'after': after, 'exact_restoration': not errors, 'errors': errors, 'physical_ddl_ms': self.ddl_ms}
        write_json(self.out / 'postflight.json', result)
        sqlrows(self.c, 'SELECT pg_advisory_unlock(20260917,23017)')
        if errors:
            raise EvidenceError(';'.join(errors))
        return result

def make_session(connection, relations, meter):
    from database.hypopg_action_scope import HypoPGActionSession
    if connection is not None:
        connection.autocommit = False

    class Session(HypoPGActionSession):

        def _fetchone(self, statement, params=None):
            is_explain = statement.startswith('EXPLAIN (FORMAT JSON) ')
            if not is_explain:
                return super()._fetchone(statement, params)
            meter.check()
            remaining = max(1, int((meter.deadline - time.monotonic()) * 1000))
            timeout = min(30000, remaining) if meter.kind == 'diagnostic' else remaining
            if meter.forced_failure == 'timeout':
                timeout = 1
            super()._fetchone('SELECT set_config(%s,%s,false)', ('statement_timeout', str(timeout)))
            token = meter.begin()
            start = time.monotonic()
            error = None
            try:
                if meter.forced_failure == 'cancel':
                    raise KeyboardInterrupt('registered injected cancellation while target scope is active')
                return super()._fetchone(statement, params)
            except BaseException as exc:
                error = exc
                if time.monotonic() >= meter.deadline:
                    raise CampaignStop('WALL_CAP_REACHED') from exc
                raise
            finally:
                meter.end(token, start, error)
                super()._fetchone('SELECT set_config(%s,%s,false)', ('statement_timeout', '10000'))

        def _evaluate(self, query, configuration):
            meter.query_info = {'exact_sql_id': query.exact_sql_hash, 'configuration_id': configuration.configuration_id}
            start = time.monotonic()
            try:
                return super()._evaluate(query, configuration)
            except Exception as exc:
                if getattr(self, 'contaminated', False):
                    raise CampaignStop('CONTAMINATED_SESSION_DISCARDED') from exc
                raise
            finally:
                meter.total_evaluate_ms += (time.monotonic() - start) * 1000
    return Session(connection, relations)

class NativeDatabase:
    """Logical what-if scope, exclusively policy-owned. No diagnostic store is reachable."""

    def __init__(self, connection, root_manager):
        self.connection = connection
        self.manager = root_manager
        self.session = None
        self.target = set()
        self.calls = []

    def exec_fetchall(self, sql):
        if not sql.lstrip().upper().startswith('SELECT'):
            raise EvidenceError('NON_CATALOG_STATEMENT')
        return sqlrows(self.connection, sql)

    def get_tables(self):
        return [r[0] for r in sqlrows(self.connection, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]

    def get_columns(self, table):
        return [r[0] for r in sqlrows(self.connection, "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (table,))]

    def bind(self, session):
        self.session = session
        self.target = set(session.physical)

    def create_index(self, t, cs):
        from tools.evaluation_substrate import IndexDefinition
        self.target.add(IndexDefinition(t, tuple(cs)))

    def drop_index(self, t, cs):
        from tools.evaluation_substrate import IndexDefinition
        k = IndexDefinition(t, tuple(cs))
        if k in self.session.protected:
            raise EvidenceError('PROTECTED_INDEX_CANNOT_BE_DISABLED')
        self.target.discard(k)
    disable_index = drop_index
    enable_index = create_index

    def drop_all_indexes(self):
        try:
            self.manager.transition(set(), 'NATIVE_TIMEOUT_RESET')
        except Exception as exc:
            raise CampaignStop('NATIVE_RESET_RESTORATION_FAILED') from exc
        if self.session:
            self.session.physical = self.session._catalog()
            self.target = set(self.session.protected)

def native_tuple(index):
    return (index.table, index.columns)

def recover_owned(manifest, directory, db):
    """Emergency restoration after a worker has exited; only its journaled index names."""
    directory = Path(directory)
    if not (directory / 'initial_catalog.json').exists():
        return {'status': 'NO_DDL_SCOPE_STARTED'}
    before = [tuple(r) for r in read(directory / 'initial_catalog.json')]
    c = connect(manifest, db, 'parent-recovery')
    start = time.monotonic()
    try:
        if not sqlrows(c, 'SELECT pg_try_advisory_lock(20260917,23017)')[0][0]:
            raise EvidenceError('RECOVERY_SCOPE_STILL_ACTIVE')
        allowed = {'mdv2_' + digest([manifest['campaign_id'], db, r['index']])[:24]: r['index'] for r in records(directory / 'ddl_events.jsonl') if r['stage'] == 'before' and r['statement'].startswith('CREATE INDEX ')}
        for row in catalog(c):
            if row in before:
                continue
            if row[2] not in allowed or row[4] or row[5]:
                raise EvidenceError('RECOVERY_FOUND_UNOWNED_OR_PROTECTED_DIFFERENCE')
            expected = allowed[row[2]]
            actual = sqlrows(c, 'SELECT ct.relname,ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum,n) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum ORDER BY k.n) FROM pg_index i JOIN pg_class ct ON ct.oid=i.indrelid WHERE i.indexrelid=%s', (row[0],))[0]
            if [actual[0], list(actual[1])] != expected:
                raise EvidenceError('RECOVERY_INDEX_DEFINITION_MISMATCH')
            with c.cursor() as q:
                q.execute('DROP INDEX public.' + qid(row[2]))
        after = catalog(c)
        if after != before:
            raise EvidenceError('RECOVERY_EXACT_RESTORATION_FAILED')
        result = {'status': 'RESTORED', 'exact_restoration': True, 'before': before, 'after': after, 'elapsed_ms': (time.monotonic() - start) * 1000}
        write_json(directory / 'emergency_postflight.json', result)
        return result
    finally:
        c.close()

def allowed_vocabulary(schema):
    return sorted(((t, cols) for t, columns in schema.items() for cols in [(c,) for c in columns] + [(a, b) for a in columns for b in columns if a != b]))
