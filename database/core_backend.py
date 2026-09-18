"""The accepted M2Q transport path with a single caller-owned QCP counter."""
import time
import logging

from database.safe_backend import NativeDatabase, CampaignStop
from tools.evaluation_substrate.schema import ConfigurationSpec, IndexDefinition, QuerySpec
from tools.evaluation_substrate.reveal import OptimizerRevealError
from adaselect_pp.common import sql_only
from adasel.measurement import QCPBudget
from database.execution_errors import classify_error


def config(items):
    return ConfigurationSpec(tuple(sorted(set(items))))


class Meter:
    """Charge at the real EXPLAIN boundary; optional observer never feeds policy."""
    def __init__(self, deadline, observer=None):
        self.deadline = time.monotonic() + max(0, deadline-time.time())
        self.kind = 'policy'
        self.observer = observer
        self.budget = QCPBudget(0)
        self.attempts = self.failures = 0
        self.explain_ms = self.total_evaluate_ms = 0.0
        self.current_window = -1
        self.purpose = 'root'
        self.query_info = {}
        self.forced_failure = None

    def check(self):
        if time.monotonic() >= self.deadline:
            raise CampaignStop('WALL_CAP_REACHED')

    def begin(self):
        self.check()
        try:
            self.budget.charge()
        except AssertionError as exc:
            raise CampaignStop('QCP_ACCOUNTING_ERROR') from exc
        self.attempts += 1
        token = dict(id=self.attempts, window=self.current_window,
                     purpose=self.purpose, query=dict(self.query_info))
        return token

    def end(self, token, start, error=None):
        elapsed = (time.monotonic()-start)*1000
        self.explain_ms += elapsed
        self.failures += int(error is not None)
        if self.observer is not None:
            try:
                self.observer(dict(token, elapsed_ms=elapsed, status='FAILED' if error else 'OK',
                                   error=str(error) if error else None))
            except Exception:
                logging.getLogger(__name__).exception('trace observer failed; no policy feedback')


class Backend(NativeDatabase):
    root_observation = None

    def bind_budget(self, budget):
        self.qcp_budget = budget
        self.session_meter.budget = budget

    def begin_root(self, workload, old):
        expected = {IndexDefinition(t, tuple(cs)) for t, cs in old} | set(self.session.protected)
        if self.target != expected or set(self.session.physical) != expected:
            raise CampaignStop('ROOT_CONFIGURATION_MISMATCH')
        self._root_epoch = self.session._capture_epoch(self.session.relevant_relations)
        self.root_observation = None

    def record_root(self, workload, costs, old):
        epoch = self.session._capture_epoch(self.session.relevant_relations)
        if epoch['epoch_hash'] != self._root_epoch['epoch_hash']:
            raise CampaignStop('ROOT_EPOCH_CHANGED')
        self.root_observation = (tuple(workload), tuple(costs), frozenset(old), epoch['epoch_hash'])

    def validate_full_add_context(self, workload, costs, old, idx):
        root = self.root_observation
        if root is None or (tuple(workload), tuple(costs), frozenset(old)) != root[:3]:
            return False
        expected = {IndexDefinition(t, tuple(cs)) for t, cs in old} | set(self.session.protected)
        if expected != set(self.session.physical) or self.target != expected:
            return False
        epoch = self.session._capture_epoch(self.session.relevant_relations)
        return epoch['epoch_hash'] == root[3]

    def calculate_now_cost(self, workload):
        total = 0.0
        self.session_meter.purpose = ('root' if self.target == set(self.session.physical)
                                     else 'ADD' if self.target > set(self.session.physical) else 'KEEP')
        for line in workload:
            self.session_meter.check()
            query = QuerySpec(str(self.session_meter.attempts), sql_only(line))
            try:
                result = self.session._evaluate(query, config(self.target))
            except Exception as exc:
                if isinstance(exc, OptimizerRevealError):
                    raise CampaignStop('SAFETY_OR_IDENTITY_FAILURE:'+str(exc)) from exc
                if classify_error(exc)['fatal']:
                    failure=CampaignStop('FATAL_OPTIMIZER_FAILURE:'+str(exc))
                    failure.execution_event=classify_error(exc)
                    raise failure from exc
                raise
            total += result.optimizer_cost
        return total
