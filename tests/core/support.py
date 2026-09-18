import ast
import copy
from pathlib import Path
import subprocess
from types import SimpleNamespace

from adasel.ada_select import AdaSelect
from adasel.measurement import QCPBudget
from adasel.policy import POLICY_DEFAULTS, PolicyConfig

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = '97d925989e53bdeea282be3b7ec66ef6c221f251'


def reference_class():
    source = subprocess.check_output(['git', 'show', REFERENCE+':adasel/ada_select.py'], cwd=ROOT).decode()
    tree = ast.parse(source)
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.module == 'adasel.config_flags')]
    ns = {}
    exec(compile(tree, 'verified_97d9259.py', 'exec'), ns)
    return ns['AdaSelect']


class Backend:
    def __init__(self, workload, roots, targets=None, old=(), cap=100):
        self.workload = list(workload)
        self.roots = list(roots)
        self.targets = targets or {}
        self.old = set(old)
        self.target = set(old)
        self.calls = []
        self.qcp_budget = QCPBudget(cap)
        self.valid = True
        self.root_recorded = False
        self.cleanups = 0

    def bind_budget(self, budget):
        self.qcp_budget = budget

    def begin_root(self, workload, old):
        self.old = set(old)
        assert self.target == self.old

    def record_root(self, workload, costs, old):
        self.root_recorded = True
        assert list(costs) == self.roots

    def create_index(self, table, cols):
        self.target.add((table, tuple(cols)))

    def drop_index(self, table, cols):
        self.target.discard((table, tuple(cols)))
        self.cleanups += 1

    disable_index = drop_index
    enable_index = create_index

    def drop_all_indexes(self):
        self.old.clear()
        self.target.clear()

    def validate_full_add_context(self, workload, costs, old, idx):
        return self.valid and list(workload) == self.workload and list(costs) == self.roots and set(old) == self.old

    def calculate_now_cost(self, workload):
        assert len(workload) == 1
        self.qcp_budget.charge()
        q = workload[0]
        self.calls.append((q, frozenset(self.target)))
        if self.target == self.old:
            value = self.roots[self.workload.index(q)]
        else:
            key = next(iter(self.target ^ self.old))
            values = self.targets.get(key, self.targets)
            value = values.get(q, self.roots[self.workload.index(q)] - 1)
        if isinstance(value, Exception):
            raise value
        return value


def tuner(backend, state=None, cls=AdaSelect):
    t = cls.__new__(cls)
    t.policy = PolicyConfig(10, backend.qcp_budget.cap, .65, 1.1)
    t.max_num, t.alpha_init, t.beta, t.timeout = 10, .65, 1.1, 30000
    for k,v in POLICY_DEFAULTS.items():setattr(t, k, v)
    t.window_id = None
    t.qcp_budget = backend.qcp_budget
    t.observer = None
    t.columns_benefit = {}
    t.workload_count = 2
    t.consecutive_timeouts = 0
    t.last_stable_config = set()
    for name in ('idx_alphas', 'idx_error_smooth', 'idx_abs_error_smooth', 'idx_seen_cnt',
                 'idx_positive_cnt', 'idx_first_seen_round', 'idx_last_seen_round', 'idx_seen_rounds',
                 'idx_last_err_sign', 'idx_sign_smooth', 'idx_last_obs_src',
                 'idx_last_observation_scope', 'idx_last_observation_kind'):
        setattr(t, name, {})
    AdaSelect._reset_round(t)
    if state:
        t.__dict__.update(copy.deepcopy(state))
    t.cost_eval = t.db_con1 = t.db_con2 = backend
    t.benefit_norm = SimpleNamespace(index_costs={}, index_costs_by_key={})
    if cls is not AdaSelect:
        t.width2_add_full_window = True
        t.lambda_policy = 'adaptive'
        t.transition_mode = 'symmetric'
        t.idx_alphas_shadow = {}
        t._m_stats = dict(what_if_calls=0, reconf_add=0, reconf_drop=0, trans_create=0., trans_drop=0.)
        t._last_structural_pair_replacement_map = {}
        t._record_shadow_action_greedy_diagnostic = lambda *a,**kw:None
        t._record_replacement_overlay = lambda selected:selected
        t._record_pair_supply_diagnostics = lambda *a,**kw:None
        t._record_materialization_gap_diagnostics = lambda *a,**kw:None
    return t


def supply(t, keys, masks, *, stage='probe', metadata=None):
    def generate(workload, old_conf=None):
        for key in keys:t.columns_benefit.setdefault(key, 0.)
        t.batch = SimpleNamespace(stage=stage, metadata=metadata or {}, scores={})
        return masks, set(keys)
    t._generate_and_merge_candidates = generate
    return t


def history(t):
    names = ['columns_benefit', 'idx_alphas', 'idx_error_smooth', 'idx_abs_error_smooth',
             'idx_seen_cnt', 'idx_positive_cnt', 'idx_last_err_sign', 'idx_sign_smooth', 'idx_last_obs_src']
    return copy.deepcopy({k:getattr(t,k) for k in names})
