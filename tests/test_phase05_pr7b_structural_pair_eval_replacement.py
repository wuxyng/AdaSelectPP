from types import MethodType, SimpleNamespace

import pytest

from adasel.ada_select import AdaSelect


A = ("t", ("a",))
B = ("t", ("b",))
C = ("t", ("c",))
PAIR = ("t", ("a", "b"))


class FakeDB:
    def __init__(self):
        self.ops = []

    def create_index(self, table, cols):
        self.ops.append(("create", table, tuple(cols)))

    def drop_index(self, table, cols):
        self.ops.append(("drop", table, tuple(cols)))

    def disable_index(self, table, cols):
        self.ops.append(("disable", table, tuple(cols)))

    def enable_index(self, table, cols):
        self.ops.append(("enable", table, tuple(cols)))


class FakeCostEval:
    def __init__(self, costs):
        self.costs = list(costs)
        self.calls = []

    def calculate_now_cost(self, workload):
        self.calls.append(list(workload))
        return self.costs.pop(0)


def _make_tuner(*, workload_count=2, gen_mode="grow", appearing=None, meta_map=None):
    tuner = AdaSelect.__new__(AdaSelect)
    appearing = set(appearing or {A, B, C, PAIR})
    meta_map = dict(meta_map or {
        A: {"family": "JOIN_EQ1", "score": 2.4},
        B: {"family": "EQ1", "score": 2.0},
        C: {"family": "EQ1", "score": 1.0},
        PAIR: {
            "family": "EQ_RANGE",
            "score": 4.2,
            "seed_key": A,
            "seed_normalized_benefit": 0.7,
            "grow_reason": "seed_eq_plus_range",
        },
    })
    tuner.columns_benefit = {A: 100.0, B: 90.0, C: 80.0, PAIR: 1.0}
    tuner.workload_count = workload_count
    tuner.ratio = 0.5
    tuner.log_candidate_sample = 12
    tuner.benefit_decay = None
    tuner.lambda_policy = "adaptive"
    tuner.alpha_init = 0.65
    tuner.rsfe_decay = 0.9
    tuner.idx_alphas = {}
    tuner.idx_error_smooth = {}
    tuner.idx_abs_error_smooth = {}
    tuner._m_stats = {"what_if_calls": 0, "candidate_count": 0, "evaluated_count": 0}
    tuner.benefit_norm = SimpleNamespace(index_costs={("a",): 0.01, ("b",): 0.01, ("c",): 0.01, ("a", "b"): 0.13})
    tuner._initial_costs = MethodType(lambda self, workload: ([100.0], 100.0), tuner)

    def fake_generate(self, workload, old_conf=None):
        self._last_wdcg_score_map = {key: float(meta_map.get(key, {}).get("score", 0.0)) for key in appearing}
        self._last_wdcg_stats = {"gen_mode": gen_mode}
        self._wdcg_gen = SimpleNamespace(enum=SimpleNamespace(last_meta=meta_map))
        return [set(appearing)], set(appearing)

    evaluated = []
    replacement = []

    def fake_test(self, idx, query_indexes, base_costs, base_total, old_conf, workload):
        evaluated.append(idx)
        self._last_obs_delta_map[idx] = 1.0

    def fake_replacement(self, idx, query_indexes, base_costs, base_total, old_conf, workload):
        replacement.append(idx)
        self._last_structural_pair_replacement_map[idx] = {"replacement_benefit": 999.0}

    tuner._generate_and_merge_candidates = MethodType(fake_generate, tuner)
    tuner._test_candidate = MethodType(fake_test, tuner)
    tuner._record_structural_pair_replacement_diagnostic = MethodType(fake_replacement, tuner)
    tuner._evaluated_log = evaluated
    tuner._replacement_log = replacement
    return tuner


def test_replacement_context_removes_only_left_prefix_single():
    ctx = AdaSelect._structural_pair_replacement_context(PAIR, {A, B, ("u", ("x",))})

    assert ctx["left_prefix_single"] == A
    assert ctx["component_singles"] == (A, B)
    assert ctx["replacement_conf"] == {B, ("u", ("x",)), PAIR}


def test_second_component_single_is_not_treated_as_left_prefix():
    ctx = AdaSelect._structural_pair_replacement_context(PAIR, {B})

    assert ctx["left_prefix_single"] == A
    assert ctx["component_singles"] == (A, B)
    assert ctx["replacement_conf"] == {B, PAIR}


def test_structural_pair_lane_evaluates_pair_outside_normal_budget():
    tuner = _make_tuner()

    tuner._estimate_benefits(["select 1"], old_conf=set())

    assert PAIR in tuner._last_evaluated_set
    assert B not in tuner._last_evaluated_set
    assert tuner._evaluated_log == [PAIR, A]
    assert tuner._replacement_log == [PAIR]
    assert tuner._m_stats["evaluated_count"] == 2
    assert tuner._last_wdcg_stats["structural_pair_quota"] == 1
    assert tuner._last_wdcg_stats["structural_pair_eval_count"] == 1
    assert tuner._last_wdcg_stats["structural_pair_eval_budgeted_out_count"] == 0


def test_structural_pair_lane_keeps_total_evaluation_budget():
    tuner = _make_tuner()

    tuner._estimate_benefits(["select 1"], old_conf=set())

    original_budget = max(1, int(tuner.ratio * len(tuner._last_appearing_set)))
    assert len(tuner._last_evaluated_set) <= original_budget
    assert tuner._m_stats["evaluated_count"] <= original_budget


def test_probe_round_behavior_has_no_structural_pair_quota():
    tuner = _make_tuner(workload_count=1, gen_mode="probe")

    tuner._estimate_benefits(["select 1"], old_conf=set())

    assert tuner._last_wdcg_stats["structural_pair_quota"] == 0
    assert tuner._last_wdcg_stats["structural_pair_eval_lane_enabled"] == 0
    assert PAIR not in tuner._last_evaluated_set
    assert tuner._evaluated_log == [A, B]


def test_replacement_benefit_is_not_used_by_config_selection():
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.columns_benefit = {A: 10.0, PAIR: 0.0}
    tuner.max_num = 10
    tuner.workload_count = 0
    tuner.transition_mode = "symmetric"
    tuner.beta = 1.1
    tuner.benefit_norm = SimpleNamespace(index_costs={("a",): 0.0, ("a", "b"): 0.0})
    tuner._m_stats = {"what_if_calls": 0, "reconf_add": 0, "reconf_drop": 0, "trans_create": 0.0, "trans_drop": 0.0}
    tuner._last_net_benefit_map = {}
    tuner._last_candidate_conf = set()
    tuner._last_final_conf = set()
    tuner._last_decision_stats = {}
    tuner._last_structural_pair_replacement_map = {PAIR: {"replacement_benefit": 1_000_000.0}}

    selected = tuner._choose_config(set())

    assert selected == {A}
    assert PAIR not in tuner._last_candidate_conf


def test_structural_pair_lane_does_not_create_candidates_from_metadata_only():
    tuner = _make_tuner(appearing={A, B, C})

    tuner._estimate_benefits(["select 1"], old_conf=set())

    assert PAIR not in tuner._last_appearing_set
    assert PAIR not in tuner._last_evaluated_set
    assert tuner._last_wdcg_stats["structural_pair_quota"] == 0


def test_replacement_diagnostic_uses_left_prefix_context_only():
    db1 = FakeDB()
    db2 = FakeDB()
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.db_con1 = db1
    tuner.db_con2 = db2
    tuner.cost_eval = FakeCostEval([70.0])
    tuner.benefit_norm = SimpleNamespace(index_costs={("a", "b"): 0.13})
    tuner.columns_benefit = {A: 100.0, B: 90.0, PAIR: 1.0}
    tuner._m_stats = {}
    tuner._last_wdcg_stats = {}
    tuner._last_obs_delta_map = {PAIR: 12.0}
    tuner._last_structural_pair_replacement_map = {}

    tuner._record_structural_pair_replacement_diagnostic(
        PAIR,
        query_indexes=[{A, PAIR}, {B}],
        base_costs=[100.0, 50.0],
        base_total=150.0,
        old_conf={A, B},
        workload=["q1", "q2"],
    )

    diag = tuner._last_structural_pair_replacement_map[PAIR]
    assert diag["left_prefix_single"] == A
    assert diag["component_singles"] == (A, B)
    assert diag["left_prefix_in_old"] is True
    assert diag["left_prefix_in_new"] is False
    assert diag["left_prefix_in_candidate"] is False
    assert diag["marginal_benefit"] == 12.0
    assert diag["replacement_benefit_raw"] == 30.0
    assert diag["replacement_benefit"] == 30.0
    assert diag["replacement_creation_cost"] == 0.13
    assert 0.0 <= diag["replacement_normalized_benefit"] <= 1.0
    assert diag["replacement_net_benefit"] == pytest.approx(
        diag["replacement_normalized_benefit"] - diag["replacement_creation_cost"]
    )
    assert diag["replacement_hit_count"] == 1
    assert diag["replacement_ok_count"] == 1
    assert diag["replacement_fail_count"] == 0
    assert tuner._m_stats["replacement_probe_count"] == 1
    assert tuner._m_stats["replacement_what_if_calls"] == 1
    assert tuner._m_stats["replacement_hit_count"] == 1
    assert tuner._m_stats["replacement_ok_count"] == 1
    assert tuner._m_stats["replacement_fail_count"] == 0
    assert tuner._last_wdcg_stats["replacement_probe_count"] == 1
    assert tuner._last_wdcg_stats["replacement_what_if_calls"] == 1
    assert ("disable", "t", ("a",)) in db2.ops
    assert ("enable", "t", ("a",)) in db2.ops
    assert ("disable", "t", ("b",)) not in db2.ops
