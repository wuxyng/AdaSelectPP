"""The four public policy inputs and versioned, non-tunable Core v2 rules."""
from dataclasses import asdict, dataclass
import math
from types import MappingProxyType

POLICY_VERSION = 'core-v2-qcp-only'
POLICY_DEFAULTS = MappingProxyType(dict(
    creation_cost_default=1., selector_epsilon=1e-9,
    full_identity_abs_tol=1e-8, full_identity_rel_tol=1e-10,
    tracking_epsilon=1e-12, tracking_decay_ceiling=.9999,
    sign_initial=.5, sign_high=.8, sign_low=.2,
    sign_high_multiplier=.7, sign_low_multiplier=1.4, tracking_threshold_min=.05,
    candidate_family_scores={'EQ_RANGE':4.5,'EQ_EQ':4.,'EQ1':3.,'JOIN_EQ1':2.4,'RANGE1':2.,'VACUUM_RESCUE1':1.4},
    candidate_source_scores={'STRONG_AST':1.,'AST':.7,'STATIC_FALLBACK':.25,'VACUUM_RESCUE':.15},
    candidate_confidence_eq=.85, candidate_confidence_join=.65, candidate_confidence_range=.65,
    candidate_confidence_rescue=.5, candidate_confidence_pair_eq=.9, candidate_confidence_pair_range=.85,
    candidate_confidence_default=.7, candidate_family_default=1.,
    candidate_width_penalty=.15, candidate_support_weight=.3, candidate_confidence_weight=.2,
    structural_priority={'JOIN_RANGE':0,'EQ_RANGE':1,'JOIN_EQ':2,'EQ_EQ':3},
    probe_rounds=2, min_width=1, max_width=2,
    candidate_topk_factor=4, candidate_topk_min_extra=6,
    candidate_per_query_cap=12, candidate_per_table_cap=4,
    candidate_round_table_cap=6, pair_supply_per_table_width2_reserve=1,
    pair_supply_round_width2_reserve=4, rsfe_decay=.9, beta_error=.2,
    lambda_min=.2, lambda_max=.95, ts_low=.5, ts_high=2.,
    ts_gate_regress=.05, ts_mad_floor_rel=1e-6, ts_sign_decay=.9,
))


def nonnegative_integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be an explicit nonnegative integer')
    return value


@dataclass(frozen=True)
class PolicyConfig:
    max_num: int
    qcp_budget: int
    alpha: float
    beta: float

    def __post_init__(self):
        nonnegative_integer(self.max_num, 'max_num')
        nonnegative_integer(self.qcp_budget, 'qcp_budget')
        for name in ('alpha', 'beta'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
        if not 0 <= self.alpha <= 1 or self.beta < 0:
            raise ValueError('require 0 <= alpha <= 1 and beta >= 0')

    @classmethod
    def from_mapping(cls, values):
        required = {'max_num', 'qcp_budget', 'alpha', 'beta'}
        if set(values) != required:
            raise ValueError(f'policy keys: missing={sorted(required-set(values))}, unsupported={sorted(set(values)-required)}')
        return cls(**values)

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class RuntimeConfig:
    """Environment inputs; none changes the candidate evaluation allowance."""
    timeout_ms: int = 30000
    indexable_columns_path: str = ''

    def __post_init__(self):
        nonnegative_integer(self.timeout_ms, 'timeout_ms')
