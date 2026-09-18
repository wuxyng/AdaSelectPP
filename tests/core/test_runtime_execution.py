"""Exercise the real runtime query builder and driver boundary, with no database."""
import json
import time
from types import SimpleNamespace
import pytest
from database.safe_backend import CampaignStop
from experiments.core_v2.release import execute_occurrence


class Connection:
    def __init__(self,payload=None,failure=None):
        self.statements=[];self.failure=failure
        self.payload=payload if payload is not None else [{'Plan':{'Actual Total Time':12.25}}]
    def cursor(self):return self
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def execute(self,statement,params=None):
        self.statements.append((statement,params))
        if statement.startswith('EXPLAIN') and self.failure:raise self.failure
    def fetchone(self):return (self.payload,)


def meter():return SimpleNamespace(check=lambda:None,deadline=time.monotonic()+60)


@pytest.mark.parametrize('sql',["SELECT 1", "WITH q AS (SELECT 1 x) SELECT x FROM q", "SELECT ';not another query'::text"])
def test_actual_runtime_uses_original_explain_analyze_prefix(sql):
    c=Connection();runtime,error=execute_occurrence(c,sql,30000,meter())
    assert runtime==12.25 and error is None
    assert c.statements==[('SELECT set_config(%s,%s,false)',('statement_timeout','30000')),
                          ('EXPLAIN (ANALYZE, FORMAT JSON) '+sql,None)]


@pytest.mark.parametrize('failure',[TypeError('unsupported analyze keyword'),ValueError('bad driver'),ConnectionError('disconnected')])
def test_programming_or_connection_error_never_becomes_timeout(failure):
    with pytest.raises(CampaignStop,match='EXECUTION_DRIVER_OR_CONNECTION_FAILURE'):
        execute_occurrence(Connection(failure=failure),'SELECT 1',30000,meter())


def test_postgres_timeout_preserves_original_penalty_and_sqlstate():
    class PostgreSQLTimeout(Exception):pgcode='57014'
    runtime,error=execute_occurrence(Connection(failure=PostgreSQLTimeout('timeout')),'SELECT 1',30000,meter())
    assert runtime==30000. and error['error']=='timeout' and error['sqlstate']=='57014'
    assert error['classification']=='PROTOCOL_TIMEOUT' and not error['fatal']


@pytest.mark.parametrize('sql',['UPDATE t SET a=1','SELECT 1; SELECT 2'])
def test_runtime_sql_validation_stops_before_any_database_request(sql):
    c=Connection()
    with pytest.raises(CampaignStop):execute_occurrence(c,sql,30000,meter())
    assert not c.statements


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1])
def test_invalid_execution_time_is_not_accepted(value):
    with pytest.raises(CampaignStop,match='INVALID_RUNTIME_OBSERVATION'):
        execute_occurrence(Connection(payload=[{'Plan':{'Actual Total Time':value}}]),'SELECT 1',30000,meter())
