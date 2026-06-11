from types import SimpleNamespace

from adasel.ada_select import AdaSelect


OLD = ("t", ("old",))
GOOD = ("t", ("good",))
EXPENSIVE = ("t", ("expensive",))
ZERO = ("t", ("zero",))
NEGATIVE = ("t", ("negative",))


def _tuner(benefits, costs, *, workload_count=1, max_num=10):
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.columns_benefit = dict(benefits)
    tuner.max_num = max_num
    tuner.workload_count = workload_count
    tuner.transition_mode = "absolute"
    tuner.beta = 1.1
    tuner.benefit_norm = SimpleNamespace(index_costs=dict(costs))
    tuner._m_stats = {
        "what_if_calls": 0,
        "reconf_add": 0,
        "reconf_drop": 0,
        "trans_create": 0.0,
        "trans_drop": 0.0,
        "filtered_nonpositive_count": 0,
    }
    tuner._last_net_benefit_map = {}
    tuner._last_candidate_conf = set()
    tuner._last_final_conf = set()
    tuner._last_decision_stats = {}
    tuner._last_structural_pair_replacement_map = {}
    tuner._last_wdcg_stats = {}
    tuner._last_appearing_set = set()
    tuner._last_evaluated_set = set()
    tuner._last_shadow_action_rows = []
    return tuner


def test_after_round0_filters_new_candidate_when_creation_cost_makes_net_nonpositive():
    tuner = _tuner(
        {
            GOOD: 100.0,
            EXPENSIVE: 10.0,
        },
        {
            ("good",): 0.0,
            ("expensive",): 1.0,
        },
    )

    tuner._choose_config(set())

    assert GOOD in tuner._last_candidate_conf
    assert EXPENSIVE not in tuner._last_candidate_conf
    assert tuner._last_net_benefit_map[EXPENSIVE] <= 0.0
    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 1.0


def test_after_round0_filters_new_zero_and_negative_benefit_candidates():
    tuner = _tuner(
        {
            GOOD: 100.0,
            ZERO: 0.0,
            NEGATIVE: -5.0,
        },
        {
            ("good",): 0.0,
            ("zero",): 0.0,
            ("negative",): 0.0,
        },
    )

    tuner._choose_config(set())

    assert tuner._last_candidate_conf == {GOOD}
    assert tuner._last_net_benefit_map[ZERO] == 0.0
    assert tuner._last_net_benefit_map[NEGATIVE] == 0.0
    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 2.0


def test_after_round0_old_conf_index_with_zero_net_may_remain_candidate():
    tuner = _tuner(
        {
            OLD: 0.0,
            GOOD: 100.0,
            ZERO: 0.0,
        },
        {
            ("old",): 99.0,
            ("good",): 0.0,
            ("zero",): 0.0,
        },
    )

    tuner._choose_config({OLD})

    assert OLD in tuner._last_candidate_conf
    assert tuner._last_net_benefit_map[OLD] == 0.0
    assert ZERO not in tuner._last_candidate_conf
    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 1.0


def test_after_round0_filtered_nonpositive_count_accumulates_metric():
    tuner = _tuner(
        {
            GOOD: 100.0,
            ZERO: 0.0,
            NEGATIVE: -5.0,
        },
        {
            ("good",): 0.0,
            ("zero",): 0.0,
            ("negative",): 0.0,
        },
    )

    tuner._choose_config(set())

    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 2.0
    assert tuner._m_stats["filtered_nonpositive_count"] == 2


def test_round0_positive_net_behavior_remains_unchanged():
    tuner = _tuner(
        {
            GOOD: 100.0,
            EXPENSIVE: 10.0,
            ZERO: 0.0,
        },
        {
            ("good",): 0.0,
            ("expensive",): 1.0,
            ("zero",): 0.0,
        },
        workload_count=0,
    )

    selected = tuner._choose_config(set())

    assert selected == {GOOD}
    assert tuner._last_candidate_conf == {GOOD}
    assert EXPENSIVE not in tuner._last_candidate_conf
    assert ZERO not in tuner._last_candidate_conf
    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 2.0
