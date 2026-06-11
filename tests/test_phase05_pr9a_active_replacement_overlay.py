from adasel.ada_select import AdaSelect
from adasel.config_flags import resolve_replacement_overlay_enabled


A = ("t", ("a",))
B = ("t", ("b",))
C = ("t", ("c",))
D = ("t", ("d",))
PAIR_AB = ("t", ("a", "b"))
PAIR_CD = ("t", ("c", "d"))
ADD_E = ("t", ("e",))


def _replace_action(pair=PAIR_AB, prefix=A, utility=0.75):
    return {
        "action_type": "REPLACE",
        "index_key": pair,
        "left_prefix_single": prefix,
        "pair_key": pair,
        "action_key": f"REPLACE:{AdaSelect._fmt_index_key(prefix)}->{AdaSelect._fmt_index_key(pair)}",
        "action_utility": utility,
    }


def _add_action(key=ADD_E, utility=0.9):
    return {
        "action_type": "ADD",
        "index_key": key,
        "action_key": f"ADD:{AdaSelect._fmt_index_key(key)}",
        "action_utility": utility,
    }


def _tuner(*, enabled=False, max_num=10):
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.replacement_overlay_enabled = enabled
    tuner.max_num = max_num
    tuner._last_wdcg_stats = {}
    tuner._last_shadow_action_rows = [_replace_action()]
    tuner._last_structural_pair_candidate_set = {PAIR_AB}
    tuner._last_structural_pair_lane_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map = {
        PAIR_AB: {
            "left_prefix_single": A,
            "component_singles": (A, B),
            "replacement_net_benefit": 0.40,
        }
    }
    tuner._m_stats = {"what_if_calls": 7}
    return tuner


def test_default_flag_false_selected_conf_unchanged():
    tuner = _tuner(enabled=False)

    selected = tuner._record_replacement_overlay({A, B})

    assert selected == {A, B}
    assert tuner._last_wdcg_stats["replacement_overlay_enabled"] == 0
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 0
    assert tuner._last_wdcg_stats["replacement_overlay_diff_from_topk_count"] == 0


def test_replacement_overlay_cli_disables_even_when_env_enabled():
    assert resolve_replacement_overlay_enabled(0, "1", True, default=False) is False


def test_replacement_overlay_cli_enables_even_when_env_disabled():
    assert resolve_replacement_overlay_enabled(1, "0", False, default=False) is True


def test_replacement_overlay_env_enables_without_cli():
    assert resolve_replacement_overlay_enabled(None, "1", False, default=False) is True


def test_replacement_overlay_defaults_disabled_without_cli_env_or_config():
    assert resolve_replacement_overlay_enabled(None, None, None, default=False) is False


def test_enabled_eligible_action_replaces_left_prefix_with_pair():
    tuner = _tuner(enabled=True)

    selected = tuner._record_replacement_overlay({A, B})

    assert selected == {PAIR_AB, B}
    assert A not in selected
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 1
    assert tuner._last_wdcg_stats["replacement_overlay_selected_action"].startswith("REPLACE:")


def test_second_component_single_is_not_removed():
    tuner = _tuner(enabled=True)

    selected = tuner._record_replacement_overlay({A, B})

    assert PAIR_AB in selected
    assert B in selected


def test_pair_already_selected_blocks_overlay():
    tuner = _tuner(enabled=True)
    tuner._overlay_opportunity_pairs = lambda _conf: {PAIR_AB}

    selected = tuner._record_replacement_overlay({A, B, C, PAIR_AB})

    assert selected == {A, B, C, PAIR_AB}
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 0
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "pair_already_selected"


def test_prefix_not_selected_blocks_overlay():
    tuner = _tuner(enabled=True)
    tuner._overlay_opportunity_pairs = lambda _conf: {PAIR_AB}

    selected = tuner._record_replacement_overlay({B, C})

    assert selected == {B, C}
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "prefix_not_in_selected"


def test_nonpositive_utility_blocks_overlay():
    tuner = _tuner(enabled=True)
    tuner._last_shadow_action_rows = [_replace_action(utility=0.0)]

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "utility_nonpositive"


def test_nonpositive_replacement_net_blocks_overlay_after_positive_utility():
    tuner = _tuner(enabled=True)
    tuner._last_structural_pair_replacement_map[PAIR_AB]["replacement_net_benefit"] = 0.0

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "net_nonpositive"


def test_lane_admitted_nonpositive_net_without_action_row_records_net_nonpositive():
    tuner = _tuner(enabled=True)
    tuner._last_shadow_action_rows = []
    tuner._last_structural_pair_replacement_map[PAIR_AB]["replacement_net_benefit"] = 0.0

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert tuner._last_wdcg_stats["overlay_opportunity_rounds"] == 1
    assert tuner._last_wdcg_stats["overlay_lane_admitted_rounds"] == 1
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 0
    assert tuner._last_wdcg_stats["replacement_overlay_blocked_count"] == 1
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "net_nonpositive"


def test_lane_admitted_positive_net_without_action_row_records_nonempty_block_reason():
    tuner = _tuner(enabled=True)
    tuner._last_shadow_action_rows = []
    tuner._last_structural_pair_replacement_map[PAIR_AB]["replacement_net_benefit"] = 0.4

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 0
    assert tuner._last_wdcg_stats["replacement_overlay_blocked_count"] == 1
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "utility_nonpositive"


def test_unrelated_replace_row_is_not_applied_without_opportunity_pair():
    tuner = _tuner(enabled=True)
    tuner._last_shadow_action_rows = [_replace_action(pair=PAIR_CD, prefix=C, utility=0.9)]
    tuner._last_structural_pair_candidate_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map = {
        PAIR_AB: {"left_prefix_single": A, "replacement_net_benefit": 0.4},
        PAIR_CD: {"left_prefix_single": C, "replacement_net_benefit": 0.7},
    }

    selected = tuner._record_replacement_overlay({A, C})

    assert selected == {A, C}
    assert PAIR_CD not in selected
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 0
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "utility_nonpositive"


def test_quota_limits_overlay_to_one_replacement():
    tuner = _tuner(enabled=True)
    tuner._last_shadow_action_rows = [
        _replace_action(pair=PAIR_AB, prefix=A, utility=0.7),
        _replace_action(pair=PAIR_CD, prefix=C, utility=0.9),
    ]
    tuner._last_structural_pair_candidate_set = {PAIR_AB, PAIR_CD}
    tuner._last_structural_pair_lane_set = {PAIR_AB, PAIR_CD}
    tuner._last_structural_pair_replacement_map[PAIR_CD] = {
        "left_prefix_single": C,
        "component_singles": (C, D),
        "replacement_net_benefit": 0.5,
    }

    selected = tuner._record_replacement_overlay({A, C})

    assert PAIR_CD in selected
    assert C not in selected
    assert A in selected
    assert PAIR_AB not in selected
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 1


def test_add_actions_are_never_applied():
    tuner = _tuner(enabled=True)
    tuner._last_shadow_action_rows = [_add_action()]

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert ADD_E not in selected
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 0


def test_no_overlay_opportunity_records_no_block_reason():
    tuner = _tuner(enabled=True)
    tuner._last_structural_pair_candidate_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map = {}
    tuner._last_shadow_action_rows = []

    selected = tuner._record_replacement_overlay({B})

    assert selected == {B}
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == ""
    assert tuner._last_wdcg_stats["replacement_overlay_blocked_count"] == 0
    assert tuner._last_wdcg_stats["overlay_opportunity_rounds"] == 0
    assert tuner._last_wdcg_stats["overlay_lane_admitted_rounds"] == 0


def test_lane_starvation_is_not_counted_as_eligibility_failure_without_admitted_pair():
    tuner = _tuner(enabled=True)
    tuner._last_structural_pair_candidate_set = {PAIR_AB}
    tuner._last_structural_pair_lane_set = set()
    tuner._last_structural_pair_replacement_map = {}
    tuner._last_shadow_action_rows = []

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 0
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "no_structural_diag_this_round"
    assert tuner._last_wdcg_stats["replacement_overlay_blocked_count"] == 1
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] not in {
        "prefix_not_in_selected",
        "pair_already_selected",
        "capacity_exceeded",
        "utility_nonpositive",
        "net_nonpositive",
    }
    assert tuner._last_wdcg_stats["overlay_opportunity_rounds"] == 1
    assert tuner._last_wdcg_stats["overlay_lane_admitted_rounds"] == 0


def test_pair_not_top_ranked_when_other_pair_has_diagnostic():
    tuner = _tuner(enabled=True)
    tuner._last_structural_pair_candidate_set = {PAIR_AB, PAIR_CD}
    tuner._last_structural_pair_lane_set = {PAIR_CD}
    tuner._last_structural_pair_replacement_map = {
        PAIR_CD: {"left_prefix_single": C, "replacement_net_benefit": 0.5}
    }
    tuner._last_shadow_action_rows = [_replace_action(pair=PAIR_CD, prefix=C, utility=0.8)]

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert tuner._last_wdcg_stats["replacement_overlay_block_reason"] == "pair_not_top_ranked_in_lane"
    assert tuner._last_wdcg_stats["replacement_overlay_blocked_count"] == 1
    assert tuner._last_wdcg_stats["overlay_opportunity_rounds"] == 1
    assert tuner._last_wdcg_stats["overlay_lane_admitted_rounds"] == 0


def test_co_residency_counted_even_when_overlay_disabled():
    tuner = _tuner(enabled=False)

    selected = tuner._record_replacement_overlay({A, PAIR_AB})

    assert selected == {A, PAIR_AB}
    assert tuner._last_wdcg_stats["replacement_overlay_co_residency_count"] == 1


def test_overlay_observability_does_not_call_candidate_generation_or_what_if():
    tuner = _tuner(enabled=True)
    tuner._generate_and_merge_candidates = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("candidate generation should not run")
    )
    tuner.cost_eval = type("FailCostEval", (), {
        "calculate_now_cost": lambda self, *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("what-if should not run")
        )
    })()

    selected = tuner._record_replacement_overlay({A})

    assert selected == {PAIR_AB}
    assert tuner._m_stats["what_if_calls"] == 7
