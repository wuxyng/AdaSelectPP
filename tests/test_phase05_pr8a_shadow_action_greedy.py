import math
from types import SimpleNamespace

import pytest

from adasel.ada_select import AdaSelect


A = ("t", ("a",))
B = ("t", ("b",))
C = ("t", ("c",))
PAIR_AB = ("t", ("a", "b"))
PAIR_AC = ("t", ("a", "c"))
ADD_D = ("t", ("d",))


def _tuner():
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.columns_benefit = {A: 100.0, B: 80.0, C: 60.0, ADD_D: 50.0, PAIR_AB: 1.0, PAIR_AC: 1.0}
    tuner.max_num = 10
    tuner.alpha_init = 0.25
    tuner.beta = 9.0
    tuner.workload_count = 1
    tuner.transition_mode = "symmetric"
    tuner.benefit_norm = SimpleNamespace(index_costs={
        ("a",): 0.01,
        ("b",): 0.01,
        ("c",): 0.01,
        ("d",): 0.10,
        ("a", "b"): 0.13,
        ("a", "c"): 0.12,
    })
    tuner._last_appearing_set = {ADD_D, PAIR_AB}
    tuner._last_evaluated_set = {PAIR_AB}
    tuner._last_candidate_conf = set()
    tuner._last_final_conf = set()
    tuner._last_net_benefit_map = {}
    tuner._last_decision_stats = {}
    tuner._last_wdcg_stats = {}
    tuner._last_shadow_action_rows = []
    tuner._m_stats = {
        "what_if_calls": 0,
        "reconf_add": 0,
        "reconf_drop": 0,
        "trans_create": 0.0,
        "trans_drop": 0.0,
        "filtered_nonpositive_count": 0,
    }
    tuner._last_structural_pair_replacement_map = {
        PAIR_AB: {
            "left_prefix_single": A,
            "component_singles": (A, B),
            "replacement_benefit_raw": 30.0,
            "replacement_normalized_benefit": 0.80,
            "replacement_creation_cost": 0.13,
            "replacement_net_benefit": 0.67,
        }
    }
    return tuner


def test_shadow_starts_from_old_conf_and_replaces_only_left_prefix():
    tuner = _tuner()
    tuner._last_appearing_set = set()
    tuner._last_evaluated_set = set()

    tuner._record_shadow_action_greedy_diagnostic(
        old_conf={A, B},
        candidate_conf=set(),
        selected_conf={A, B},
        norm_map={},
        net_map={},
    )

    assert tuner._last_wdcg_stats["shadow_greedy_config_conflict_aware"] == "t(a,b);t(b)"
    assert PAIR_AB in _parse_conf_for_test(tuner._last_wdcg_stats["shadow_greedy_config_conflict_aware"])
    assert B in _parse_conf_for_test(tuner._last_wdcg_stats["shadow_greedy_config_conflict_aware"])
    assert A not in _parse_conf_for_test(tuner._last_wdcg_stats["shadow_greedy_config_conflict_aware"])
    assert tuner._last_wdcg_stats["shadow_replacement_count"] == 1
    assert tuner._last_wdcg_stats["shadow_transition_drop_count"] == 1


def test_second_component_single_is_not_removed_by_replace():
    tuner = _tuner()

    conflict_conf, _, stats = tuner._apply_shadow_actions_conflict_aware(
        {A, B},
        tuner._build_shadow_action_table({A, B}, set(), norm_map={}, net_map={}),
    )

    assert PAIR_AB in conflict_conf
    assert B in conflict_conf
    assert A not in conflict_conf
    assert stats["shadow_replacement_count"] == 1


def test_naive_converts_missing_prefix_replace_into_stale_add():
    tuner = _tuner()
    tuner._last_appearing_set = set()
    tuner._last_evaluated_set = set()
    actions = tuner._build_shadow_action_table({B}, set(), norm_map={}, net_map={})

    naive_conf, naive_actions, naive_stats = tuner._apply_shadow_actions_naive({B}, actions)
    conflict_conf, conflict_actions, conflict_stats = tuner._apply_shadow_actions_conflict_aware({B}, actions)

    assert PAIR_AB in naive_conf
    assert B in naive_conf
    assert naive_stats["naive_prefix_missing_add_count"] == 1
    assert any(a.get("stale_replacement_converted_to_add") == 1 for a in naive_actions)
    assert PAIR_AB not in conflict_conf
    assert conflict_actions == []
    assert conflict_stats["stale_prefix_missing_count"] == 1


def test_two_replacements_competing_for_same_prefix_diverge():
    tuner = _tuner()
    tuner._last_structural_pair_replacement_map[PAIR_AC] = {
        "left_prefix_single": A,
        "component_singles": (A, C),
        "replacement_benefit_raw": 40.0,
        "replacement_normalized_benefit": 0.79,
        "replacement_creation_cost": 0.12,
        "replacement_net_benefit": 0.67,
    }
    tuner._last_appearing_set = set()
    tuner._last_evaluated_set = set()
    actions = tuner._build_shadow_action_table({A}, set(), norm_map={}, net_map={})

    naive_conf, naive_actions, _ = tuner._apply_shadow_actions_naive({A}, actions)
    conflict_conf, conflict_actions, stats = tuner._apply_shadow_actions_conflict_aware({A}, actions)

    assert {PAIR_AB, PAIR_AC}.issubset(naive_conf)
    assert len([k for k in conflict_conf if k in {PAIR_AB, PAIR_AC}]) == 1
    assert len(naive_actions) == 2
    assert len(conflict_actions) == 1
    assert stats["stale_prefix_missing_count"] == 1


def test_add_and_replace_rows_share_utility_scale_basis():
    tuner = _tuner()
    rows = tuner._build_shadow_action_table({A}, {ADD_D}, norm_map={ADD_D: 0.5}, net_map={})
    bases = {row["utility_scale_basis"] for row in rows if row["action_type"] in {"ADD", "REPLACE"}}

    assert len(bases) == 1
    assert {row["action_type"] for row in rows} == {"ADD", "REPLACE"}


def test_alpha_beta_are_context_not_utility_weights():
    tuner = _tuner()
    tuner.alpha_init = 0.01
    tuner.beta = 100.0
    row = tuner._shadow_action_row_for_replace(PAIR_AB, tuner._last_structural_pair_replacement_map[PAIR_AB], utility_scale_basis=100.0)

    assert row["alpha_context"] == 0.01
    assert row["beta_context"] == 100.0
    assert row["benefit_weight"] == 1.0
    assert row["transition_weight"] == 1.0
    assert row["action_utility"] == pytest.approx(math.log1p(30.0) / math.log1p(100.0) - 0.13)


def test_replace_uses_gross_raw_benefit_on_shared_scale_not_replacement_net_as_benefit():
    tuner = _tuner()
    row = tuner._shadow_action_row_for_replace(PAIR_AB, tuner._last_structural_pair_replacement_map[PAIR_AB], utility_scale_basis=100.0)
    expected = math.log1p(30.0) / math.log1p(100.0)

    assert row["action_normalized_benefit"] == pytest.approx(expected)
    assert row["action_normalized_benefit"] != tuner._last_structural_pair_replacement_map[PAIR_AB]["replacement_net_benefit"]
    assert row["action_normalized_benefit"] != tuner._last_structural_pair_replacement_map[PAIR_AB]["replacement_normalized_benefit"]
    assert row["action_utility"] == pytest.approx(expected - 0.13)
    assert row["action_utility"] != pytest.approx(0.67 - 0.13)


def test_add_gross_benefit_uses_raw_benefit_on_shared_scale():
    tuner = _tuner()
    row = tuner._shadow_action_row_for_add(
        ADD_D,
        norm_map={},
        net_map={ADD_D: 0.23},
        utility_scale_basis=100.0,
    )
    expected = math.log1p(50.0) / math.log1p(100.0)

    assert row["utility_source"] == "raw_benefit_shared_log_scale"
    assert row["action_benefit_raw"] == 50.0
    assert row["action_normalized_benefit"] == pytest.approx(expected)
    assert row["action_utility"] == pytest.approx(expected - 0.10)


def test_add_and_replace_normalize_with_same_shared_basis_from_raw_benefits():
    tuner = _tuner()
    tuner.columns_benefit = {ADD_D: 9.0}
    tuner._last_appearing_set = {ADD_D, PAIR_AB}
    tuner._last_evaluated_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map[PAIR_AB]["replacement_benefit_raw"] = 999.0
    tuner._last_structural_pair_replacement_map[PAIR_AB]["replacement_normalized_benefit"] = 0.42
    tuner._last_structural_pair_replacement_map[PAIR_AB]["replacement_net_benefit"] = 0.29

    rows = tuner._build_shadow_action_table({A}, {ADD_D}, norm_map={ADD_D: 1.0}, net_map={})
    add_row = next(row for row in rows if row["action_type"] == "ADD" and row["index_key"] == ADD_D)
    replace_row = next(row for row in rows if row["action_type"] == "REPLACE" and row["index_key"] == PAIR_AB)
    shared_basis = replace_row["utility_scale_basis"]

    assert add_row["utility_scale_basis"] == shared_basis
    assert shared_basis == 999.0
    assert replace_row["action_normalized_benefit"] == pytest.approx(1.0)
    assert add_row["action_normalized_benefit"] < 1.0
    assert add_row["action_normalized_benefit"] == pytest.approx(math.log1p(9.0) / math.log1p(shared_basis))


def test_keep_rows_are_not_part_of_greedy_apply_loop():
    tuner = _tuner()
    actions = tuner._build_shadow_action_table({A, B}, {ADD_D}, norm_map={ADD_D: 0.4}, net_map={})
    naive_conf, naive_actions, _ = tuner._apply_shadow_actions_naive({A, B}, actions)

    assert all(a["action_type"] != "KEEP" for a in actions)
    assert all(a["action_type"] != "KEEP" for a in naive_actions)
    assert B in naive_conf


def test_full_capacity_add_is_skipped_without_incrementing_counters():
    tuner = _tuner()
    tuner.max_num = 1
    action = tuner._shadow_action_row_for_add(ADD_D, norm_map={}, net_map={}, utility_scale_basis=100.0)

    naive_conf, naive_actions, naive_stats = tuner._apply_shadow_actions_naive({A}, [action])
    conflict_conf, conflict_actions, conflict_stats = tuner._apply_shadow_actions_conflict_aware({A}, [action])

    assert naive_conf == {A}
    assert conflict_conf == {A}
    assert naive_actions == []
    assert conflict_actions == []
    assert naive_stats["naive_add_count"] == 0
    assert conflict_stats["shadow_transition_add_count"] == 0


def test_full_capacity_naive_missing_prefix_replace_is_not_counted_as_add():
    tuner = _tuner()
    tuner.max_num = 1
    action = tuner._shadow_action_row_for_replace(
        PAIR_AB,
        tuner._last_structural_pair_replacement_map[PAIR_AB],
        utility_scale_basis=100.0,
    )

    naive_conf, naive_actions, naive_stats = tuner._apply_shadow_actions_naive({B}, [action])

    assert naive_conf == {B}
    assert naive_actions == []
    assert PAIR_AB not in naive_conf
    assert naive_stats["naive_prefix_missing_add_count"] == 0
    assert naive_stats["naive_add_count"] == 0


def test_choose_config_active_selected_conf_remains_unchanged_by_shadow_replacement():
    tuner = _tuner()
    tuner.columns_benefit = {A: 100.0, PAIR_AB: 0.0}
    tuner.workload_count = 0
    tuner.max_num = 10
    tuner._last_appearing_set = {PAIR_AB}
    tuner._last_evaluated_set = {PAIR_AB}

    selected = tuner._choose_config(set())

    assert selected == {A}
    assert PAIR_AB not in selected
    assert PAIR_AB not in tuner._last_candidate_conf
    assert "shadow_greedy_config_conflict_aware" in tuner._last_wdcg_stats


def test_shadow_helpers_do_not_call_candidate_generation():
    tuner = _tuner()

    def fail_generate(*_args, **_kwargs):
        raise AssertionError("candidate generation should not be called by shadow diagnostics")

    tuner._generate_and_merge_candidates = fail_generate
    tuner._record_shadow_action_greedy_diagnostic(
        old_conf={A},
        candidate_conf={ADD_D},
        selected_conf={A},
        norm_map={ADD_D: 0.4},
        net_map={},
    )

    assert tuner._last_wdcg_stats["shadow_action_count"] >= 1


def _parse_conf_for_test(text):
    result = set()
    for item in str(text).split(";"):
        item = item.strip()
        if not item:
            continue
        table, rest = item.split("(", 1)
        cols = tuple(c.strip() for c in rest.rstrip(")").split(",") if c.strip())
        result.add((table, cols))
    return result
