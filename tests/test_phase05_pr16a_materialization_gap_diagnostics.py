import csv
from types import SimpleNamespace

from adasel.ada_select import AdaSelect
from util.metrics_recorder import MetricsRecorder
from util.trace_recorder import TraceRecorder


A = ("t", ("a",))
B = ("t", ("b",))
PAIR_AB = ("t", ("a", "b"))
PAIR_AC = ("t", ("a", "c"))


def _tuner(*, postround=None, targets=None):
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.columns_benefit = {
        PAIR_AB: 10.0,
        PAIR_AC: 7.0,
        A: 5.0,
    }
    tuner.benefit_norm = SimpleNamespace(index_costs={("a", "b"): 0.11, ("a", "c"): 0.13, ("a",): 0.01})
    tuner._last_wdcg_stats = {}
    tuner._last_materialization_gap_map = {}
    tuner._last_evaluated_set = set()
    tuner._last_structural_pair_replacement_map = {}
    tuner._last_shadow_action_rows = []
    tuner._last_overlay_fired_pairs = set()
    tuner.target_pair_audit = set(targets if targets is not None else {PAIR_AB})
    tuner._wdcg_gen = SimpleNamespace(last_pair_supply={
        "postround_width2": set(postround if postround is not None else {PAIR_AB}),
    })
    tuner._m_stats = {"what_if_calls": 0}
    return tuner


def _record(
    tuner,
    *,
    old_conf=None,
    candidate_conf=None,
    selected_conf=None,
    final_conf=None,
    norm=None,
    net=None,
):
    old = set(old_conf or set())
    candidate = set(candidate_conf or set())
    selected = set(selected_conf if selected_conf is not None else candidate)
    final = set(final_conf if final_conf is not None else selected)
    tuner._record_materialization_gap_diagnostics(
        old_conf=old,
        candidate_conf=candidate,
        selected_conf=selected,
        final_conf=final,
        norm_map=dict(norm or {PAIR_AB: 0.5, PAIR_AC: 0.4, A: 0.3}),
        net_map=dict(net or {PAIR_AB: 0.1, PAIR_AC: 0.1, A: 0.2}),
    )
    return tuner._last_materialization_gap_map[PAIR_AB]


def test_postround_pair_not_evaluated_not_final_is_eval_gap():
    tuner = _tuner()

    diag = _record(tuner, final_conf=set())

    assert diag["mat_gap_reason"] == "eval_gap"
    assert diag["mat_pair_in_postround"] == 1
    assert diag["mat_pair_evaluated"] == 0
    assert tuner._last_wdcg_stats["materialization_gap_eval_gap_count"] == 1
    assert tuner._last_wdcg_stats["materialization_gap_eval_gap_examples"] == "t(a,b)"


def test_evaluated_pair_with_prefix_and_positive_replacement_is_prefix_shadowing():
    tuner = _tuner()
    tuner._last_evaluated_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map = {
        PAIR_AB: {"replacement_net_benefit": 0.25, "left_prefix_single": A}
    }

    diag = _record(tuner, old_conf={A}, candidate_conf={A}, final_conf={A}, net={PAIR_AB: -0.03, A: 0.2})

    assert diag["mat_gap_reason"] == "prefix_shadowing_likely"
    assert diag["mat_left_prefix"] == "t(a)"
    assert diag["mat_left_prefix_in_old_conf"] == 1
    assert diag["mat_replacement_net_benefit"] == 0.25
    assert tuner._last_wdcg_stats["materialization_gap_prefix_shadowing_likely_count"] == 1


def test_evaluated_pair_without_prefix_and_positive_replacement_is_main_nonpositive():
    tuner = _tuner()
    tuner._last_evaluated_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map = {
        PAIR_AB: {"replacement_net_benefit": 0.25, "left_prefix_single": A}
    }

    diag = _record(tuner, old_conf={B}, candidate_conf={B}, final_conf={B}, net={PAIR_AB: 0.0, B: 0.2})

    assert diag["mat_gap_reason"] == "replacement_positive_main_nonpositive"
    assert tuner._last_wdcg_stats["materialization_gap_replacement_positive_main_nonpositive_count"] == 1


def test_main_positive_pair_absent_from_candidate_and_final_is_not_selected():
    tuner = _tuner()
    tuner._last_evaluated_set = {PAIR_AB}

    diag = _record(tuner, candidate_conf={A}, final_conf={A}, net={PAIR_AB: 0.2, A: 0.1})

    assert diag["mat_gap_reason"] == "main_positive_but_not_selected"
    assert tuner._last_wdcg_stats["materialization_gap_main_positive_but_not_selected_count"] == 1


def test_candidate_pair_rejected_when_beta_gate_kept_old_conf():
    tuner = _tuner()
    tuner._last_evaluated_set = {PAIR_AB}

    diag = _record(tuner, old_conf={A}, candidate_conf={PAIR_AB}, selected_conf={A}, final_conf={A}, net={PAIR_AB: 0.2, A: 0.1})

    assert diag["mat_gap_reason"] == "candidate_conf_rejected_by_beta"
    assert tuner._last_wdcg_stats["materialization_gap_candidate_conf_rejected_by_beta_count"] == 1


def test_final_pair_is_already_final():
    tuner = _tuner()
    tuner._last_evaluated_set = {PAIR_AB}

    diag = _record(tuner, candidate_conf={PAIR_AB}, final_conf={PAIR_AB})

    assert diag["mat_gap_reason"] == "already_final"
    assert tuner._last_wdcg_stats["materialization_gap_already_final_count"] == 1


def test_overlay_applied_takes_precedence_over_already_final():
    tuner = _tuner()
    tuner._last_evaluated_set = {PAIR_AB}
    tuner._last_overlay_fired_pairs = {PAIR_AB}

    diag = _record(tuner, candidate_conf={PAIR_AB}, final_conf={PAIR_AB})

    assert diag["mat_gap_reason"] == "overlay_applied"
    assert tuner._last_wdcg_stats["materialization_gap_overlay_applied_count"] == 1


def test_target_pair_not_in_postround_is_not_postround():
    tuner = _tuner(postround=set(), targets={PAIR_AB})

    diag = _record(tuner, final_conf=set())

    assert diag["mat_gap_reason"] == "not_postround"
    assert tuner._last_wdcg_stats["materialization_gap_not_postround_count"] == 1
    assert tuner._last_wdcg_stats["materialization_gap_not_postround_examples"] == "t(a,b)"
    assert tuner._last_wdcg_stats["materialization_gap_pair_count"] == 1
    reason_count_sum = sum(
        int(tuner._last_wdcg_stats[f"materialization_gap_{reason}_count"])
        for reason in (
            "not_postround",
            "eval_gap",
            "prefix_shadowing_likely",
            "replacement_positive_main_nonpositive",
            "eval_confirmed_nonbeneficial",
            "main_positive_but_not_selected",
            "candidate_conf_rejected_by_beta",
            "already_final",
            "overlay_applied",
            "unknown",
        )
    )
    assert reason_count_sum == tuner._last_wdcg_stats["materialization_gap_pair_count"]


def test_replacement_utility_is_recorded_from_existing_shadow_row():
    tuner = _tuner()
    tuner._last_evaluated_set = {PAIR_AB}
    tuner._last_structural_pair_replacement_map = {
        PAIR_AB: {"replacement_net_benefit": 0.25, "left_prefix_single": A}
    }
    tuner._last_shadow_action_rows = [
        {"action_type": "REPLACE", "index_key": PAIR_AB, "action_utility": 0.17}
    ]

    diag = _record(tuner, old_conf={A}, candidate_conf={A}, final_conf={A}, net={PAIR_AB: -0.03, A: 0.2})

    assert diag["mat_replacement_utility"] == 0.17


def test_diagnostics_do_not_change_configs_or_add_what_if_calls():
    tuner = _tuner()
    old = {A}
    candidate = {PAIR_AB}
    selected = {A}
    final = {A}
    before_stats = dict(tuner._m_stats)

    _record(tuner, old_conf=old, candidate_conf=candidate, selected_conf=selected, final_conf=final)

    assert old == {A}
    assert candidate == {PAIR_AB}
    assert selected == {A}
    assert final == {A}
    assert tuner._m_stats == before_stats


def test_materialization_gap_metrics_are_serialized(tmp_path):
    path = tmp_path / "metrics.csv"
    recorder = MetricsRecorder(str(path), flush_each_row=False)
    recorder.record_round(
        round_id=0,
        old_conf=set(),
        new_conf=set(),
        materialization_gap_pair_count=2,
        materialization_gap_not_postround_count=1,
        materialization_gap_eval_gap_count=1,
        materialization_gap_not_postround_examples="t(a,c)",
        materialization_gap_eval_gap_examples="t(a,b)",
    )
    recorder.close()

    with path.open(newline="", encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
    assert row["materialization_gap_pair_count"] == "2"
    assert row["materialization_gap_not_postround_count"] == "1"
    assert row["materialization_gap_eval_gap_count"] == "1"
    assert row["materialization_gap_not_postround_examples"] == "t(a,c)"
    assert row["materialization_gap_eval_gap_examples"] == "t(a,b)"


def test_trace_records_materialization_gap_fields(tmp_path):
    path = tmp_path / "trace.csv"
    tuner = _tuner()
    tuner._last_materialization_gap_map = {
        PAIR_AB: {
            "mat_pair_key": "t(a,b)",
            "mat_pair_in_postround": 1,
            "mat_pair_in_candidate_conf": 0,
            "mat_pair_in_final_conf": 0,
            "mat_pair_evaluated": 0,
            "mat_pair_main_raw_benefit": 10.0,
            "mat_pair_main_normalized_benefit": 0.5,
            "mat_pair_main_net_utility": 0.1,
            "mat_pair_creation_cost": 0.11,
            "mat_replacement_diag_available": 0,
            "mat_gap_reason": "eval_gap",
        }
    }
    tuner._last_wdcg_stats = {
        "materialization_gap_pair_count": 1,
        "materialization_gap_eval_gap_count": 1,
        "materialization_gap_not_postround_count": 0,
        "materialization_gap_eval_gap_examples": "t(a,b)",
    }
    tuner._last_final_conf = set()
    tuner._last_appearing_set = set()
    tuner._last_candidate_conf = set()
    tuner._last_evaluated_set = set()

    with TraceRecorder(path, flush_each_row=False) as tracer:
        tracer.record_round(0, old_conf=set(), new_conf=set(), tuner=tuner)

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["mat_pair_key"] == "t(a,b)"
    assert rows[0]["mat_gap_reason"] == "eval_gap"
    assert rows[0]["materialization_gap_pair_count"] == "1"
    assert rows[0]["materialization_gap_not_postround_count"] == "0"
