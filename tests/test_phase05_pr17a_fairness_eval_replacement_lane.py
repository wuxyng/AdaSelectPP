from types import SimpleNamespace

from adasel.ada_select import AdaSelect


A = ("t", ("a",))
B = ("t", ("b",))
PAIR_AB = ("t", ("a", "b"))
PAIR_AC = ("t", ("a", "c"))
PAIR_BC = ("t", ("b", "c"))
LINEITEM_SHIPDATE = ("lineitem", ("l_partkey", "l_shipdate"))
ORDERS_PAIR = ("orders", ("o_custkey", "o_orderdate"))
ORDERS_PREFIX = ("orders", ("o_custkey",))


def _tuner(*, enabled=True, quota=1, postround=None, fairness_added=None, targets=None):
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.fairness_eval_lane_enabled = enabled
    tuner.fairness_eval_lane_quota = quota
    tuner.target_pair_audit = set(targets or set())
    tuner._last_wdcg_stats = {}
    tuner._last_evaluated_set = set()
    tuner._last_eval_order = []
    tuner._last_structural_pair_replacement_map = {}
    tuner._last_shadow_action_rows = []
    tuner._last_overlay_fired_pairs = set()
    tuner._last_materialization_gap_map = {}
    tuner._last_obs_delta_map = {}
    tuner._m_stats = {"what_if_calls": 0}
    tuner.columns_benefit = {}
    tuner.benefit_norm = SimpleNamespace(index_costs={})
    tuner._creation_cost = lambda _key: 0.0
    tuner._wdcg_gen = SimpleNamespace(last_pair_supply={
        "postround_width2": set(postround if postround is not None else {PAIR_AB}),
        "fairness_added_round_width2": set(fairness_added if fairness_added is not None else set()),
    })
    return tuner


def test_enabled_lane_evaluates_target_pair_and_runs_replacement_after_test():
    tuner = _tuner(
        enabled=True,
        quota=1,
        postround={PAIR_AB},
        fairness_added={PAIR_AB},
        targets={PAIR_AB},
    )
    calls = []

    def fake_test(pair, *_args):
        calls.append(("test", pair))
        tuner._last_obs_delta_map[pair] = -1.0
        tuner._m_stats["what_if_calls"] += 3

    def fake_replacement(pair, *_args):
        calls.append(("replacement", pair, tuner._last_obs_delta_map.get(pair)))
        tuner._last_structural_pair_replacement_map[pair] = {
            "left_prefix_single": A,
            "replacement_net_benefit": 0.5,
        }
        tuner._last_wdcg_stats["replacement_what_if_calls"] = (
            tuner._last_wdcg_stats.get("replacement_what_if_calls", 0) + 2
        )

    tuner._test_candidate = fake_test
    tuner._record_structural_pair_replacement_diagnostic = fake_replacement

    count = tuner._run_fairness_eval_lane([], [], 0.0, {A}, [])

    assert count == 1
    assert calls == [("test", PAIR_AB), ("replacement", PAIR_AB, -1.0)]
    assert PAIR_AB in tuner._last_evaluated_set
    assert tuner._last_wdcg_stats["fairness_eval_lane_evaluated_pairs"] == "t(a,b)"
    assert tuner._last_wdcg_stats["fairness_eval_lane_what_if_calls"] == 3
    assert tuner._last_wdcg_stats["fairness_eval_lane_replacement_what_if_calls"] == 2
    assert tuner._last_wdcg_stats["fairness_eval_lane_shadowing_revealed_count"] == 1


def test_quota_is_respected_and_target_priority_beats_fairness_order():
    tuner = _tuner(
        enabled=True,
        quota=1,
        postround={PAIR_AB, LINEITEM_SHIPDATE, ORDERS_PAIR},
        fairness_added={PAIR_AB, ORDERS_PAIR},
        targets={ORDERS_PAIR, LINEITEM_SHIPDATE},
    )
    evaluated = []

    def fake_test(pair, *_args):
        evaluated.append(pair)

    def fake_replacement(pair, *_args):
        tuner._last_structural_pair_replacement_map[pair] = {"replacement_net_benefit": 0.0}

    tuner._test_candidate = fake_test
    tuner._record_structural_pair_replacement_diagnostic = fake_replacement

    tuner._run_fairness_eval_lane([], [], 0.0, set(), [])

    assert evaluated == [LINEITEM_SHIPDATE]
    assert tuner._last_wdcg_stats["fairness_eval_lane_candidate_count"] == 3
    assert tuner._last_wdcg_stats["fairness_eval_lane_evaluated_count"] == 1
    assert tuner._last_wdcg_stats["fairness_eval_lane_budgeted_out_count"] == 2


def test_already_evaluated_pairs_are_skipped():
    tuner = _tuner(enabled=True, quota=2, postround={PAIR_AB, PAIR_AC})
    tuner._last_evaluated_set = {PAIR_AB}
    evaluated = []
    tuner._test_candidate = lambda pair, *_args: evaluated.append(pair)
    tuner._record_structural_pair_replacement_diagnostic = lambda pair, *_args: None

    tuner._run_fairness_eval_lane([], [], 0.0, set(), [])

    assert evaluated == [PAIR_AC]
    assert tuner._last_wdcg_stats["fairness_eval_lane_skipped_already_evaluated_count"] == 1


def test_disabled_lane_does_not_change_sets_replacement_map_or_what_if_counts():
    tuner = _tuner(enabled=False, quota=2, postround={PAIR_AB, PAIR_AC})
    tuner._last_evaluated_set = {PAIR_AB}
    before_eval = set(tuner._last_evaluated_set)
    before_map = dict(tuner._last_structural_pair_replacement_map)
    before_what_if = tuner._m_stats["what_if_calls"]
    tuner._test_candidate = lambda *_args: (_ for _ in ()).throw(AssertionError("should not evaluate"))
    tuner._record_structural_pair_replacement_diagnostic = lambda *_args: (_ for _ in ()).throw(AssertionError("should not replace"))

    count = tuner._run_fairness_eval_lane([], [], 0.0, set(), [])

    assert count == 0
    assert tuner._last_evaluated_set == before_eval
    assert tuner._last_structural_pair_replacement_map == before_map
    assert tuner._m_stats["what_if_calls"] == before_what_if
    assert tuner._last_wdcg_stats["fairness_eval_lane_enabled"] == 0
    assert tuner._last_wdcg_stats["fairness_eval_lane_evaluated_count"] == 0


def test_materialization_classifies_shadowing_after_lane_replacement_diagnostic():
    tuner = _tuner(enabled=True, postround={ORDERS_PAIR}, targets={ORDERS_PAIR})
    tuner.columns_benefit = {ORDERS_PAIR: 0.0}
    tuner._last_evaluated_set = {ORDERS_PAIR}
    tuner._last_structural_pair_replacement_map = {
        ORDERS_PAIR: {
            "left_prefix_single": ORDERS_PREFIX,
            "replacement_net_benefit": 0.4,
        }
    }

    tuner._record_materialization_gap_diagnostics(
        old_conf={ORDERS_PREFIX},
        candidate_conf={ORDERS_PREFIX},
        selected_conf={ORDERS_PREFIX},
        final_conf={ORDERS_PREFIX},
        norm_map={ORDERS_PAIR: 0.0},
        net_map={ORDERS_PAIR: -0.1, ORDERS_PREFIX: 0.2},
    )

    assert tuner._last_materialization_gap_map[ORDERS_PAIR]["mat_gap_reason"] == "prefix_shadowing_likely"
    assert tuner._last_wdcg_stats["materialization_gap_prefix_shadowing_likely_count"] == 1


def test_materialization_classifies_eval_confirmed_nonbeneficial():
    tuner = _tuner(enabled=True, postround={PAIR_AB}, targets={PAIR_AB})
    tuner.columns_benefit = {PAIR_AB: 0.0}
    tuner._last_evaluated_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map = {
        PAIR_AB: {
            "left_prefix_single": A,
            "replacement_net_benefit": 0.0,
        }
    }

    tuner._record_materialization_gap_diagnostics(
        old_conf={A},
        candidate_conf={A},
        selected_conf={A},
        final_conf={A},
        norm_map={PAIR_AB: 0.0},
        net_map={PAIR_AB: 0.0, A: 0.2},
    )

    assert tuner._last_materialization_gap_map[PAIR_AB]["mat_gap_reason"] == "eval_confirmed_nonbeneficial"
    assert tuner._last_wdcg_stats["materialization_gap_eval_confirmed_nonbeneficial_count"] == 1
    assert tuner._last_wdcg_stats["materialization_gap_eval_confirmed_nonbeneficial_examples"] == "t(a,b)"


def test_overlay_logic_remains_unchanged_by_disabled_lane():
    tuner = _tuner(enabled=False, postround={PAIR_AB})
    tuner.replacement_overlay_enabled = True
    tuner.max_num = 2
    tuner._last_structural_pair_candidate_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map = {}
    tuner._last_shadow_action_rows = []

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert tuner._last_wdcg_stats["replacement_overlay_applied_count"] == 0


def test_generator_exposes_fairness_added_pairs_without_changing_selection_diag():
    supply = {"fairness_added_round_width2": {PAIR_BC}, "postround_width2": {PAIR_AB, PAIR_BC}}
    tuner = _tuner(enabled=True, quota=1, postround=supply["postround_width2"], fairness_added=supply["fairness_added_round_width2"])

    ranked = tuner._rank_fairness_eval_lane_candidates({PAIR_AB, PAIR_BC})

    assert ranked[0] == PAIR_BC
