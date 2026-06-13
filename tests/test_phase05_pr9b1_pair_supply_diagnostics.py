import csv

from adasel.ada_select import AdaSelect
from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from adaselect_pp.candidate_gen_v2.types import Candidate, SeedState
from util.metrics_recorder import MetricsRecorder
from util.trace_recorder import TraceRecorder


A = ("t", ("a",))
B = ("t", ("b",))
C = ("t", ("c",))
D = ("t", ("d",))
PAIR_AB = ("t", ("a", "b"))
PAIR_AC = ("t", ("a", "c"))
PAIR_BC = ("t", ("b", "c"))
PAIR_AD = ("t", ("a", "d"))


def _candidate(key, family="EQ1", support=1):
    cand = Candidate(key=key, family=family, source="AST", confidence=0.9, roles=("test",))
    cand.query_ids = {0}
    cand.template_ids = {"q0"}
    cand.support_count = support
    return cand


def _generator(per_query_cap=2, per_table_cap=10, round_table_cap=10):
    gen = MCIGCandidateGenerator.__new__(MCIGCandidateGenerator)
    gen.per_query_cap = per_query_cap
    gen.per_table_cap = per_table_cap
    gen.round_table_cap = round_table_cap
    return gen


def test_width_first_per_query_cap_records_dropped_width2_without_changing_selection():
    gen = _generator(per_query_cap=2)
    out = {
        A: _candidate(A, "EQ1"),
        B: _candidate(B, "EQ1"),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", support=10),
    }

    selected_legacy = gen._query_reduce(out)
    selected_diag, diag = gen._query_reduce_with_diagnostics(out)

    assert selected_diag == selected_legacy
    assert set(selected_diag) == {A, B}
    assert diag["width2_before"] == {PAIR_AB}
    assert diag["width2_after"] == set()
    assert diag["width2_dropped"] == {PAIR_AB}


def test_round_cap_diagnostics_preserve_round_selection_and_rank_width1_ahead():
    gen = _generator(round_table_cap=10)
    merged = {
        A: _candidate(A, "EQ1"),
        B: _candidate(B, "JOIN_EQ1"),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", support=20),
    }
    for cand in merged.values():
        cand.score = gen._score(cand)

    selected, diag = gen._round_select_with_diagnostics(merged, topk=2)

    assert [cand.key for cand in selected] == [A, B]
    assert diag["width2_before"] == {PAIR_AB}
    assert diag["width2_after"] == set()
    assert diag["width2_dropped"] == {PAIR_AB}
    assert diag["width1_ranked_ahead_of_best_width2"] == 2
    assert diag["best_width2_family_score"] == gen.FAMILY_SCORE["EQ_RANGE"]
    assert diag["max_family_score_of_displacing_width1"] == gen.FAMILY_SCORE["EQ1"]


def test_grow_seed_family_is_preserved_and_mismatch_counters_are_recorded():
    gen = _generator()
    seed = SeedState(key=A, evaluated_count=1, positive_count=1, benefit=10.0, normalized_benefit=0.8, mature=True)
    grow_meta = {}

    gen._record_grow_meta(grow_meta, PAIR_AB, seed, "seed_eq_plus_range", "JOIN_EQ1")

    pair_cand = _candidate(PAIR_AB, "EQ_RANGE")
    pair_cand.score = gen._score(pair_cand)
    meta_map = {PAIR_AB: gen._candidate_meta(pair_cand)}
    meta_map[PAIR_AB].update(grow_meta[PAIR_AB])
    stats = gen._annotate_pair_fidelity(meta_map)

    assert meta_map[PAIR_AB]["grow_seed_key"] == A
    assert meta_map[PAIR_AB]["grow_seed_family"] == "JOIN_EQ1"
    assert meta_map[PAIR_AB]["grow_seed_family_set"] == ["JOIN_EQ1"]
    assert stats["pair_family_vs_grow_reason_mismatch"] == 1
    assert stats["seed_family_missing_count"] == 0
    assert stats["join_seed_downgraded_count"] == 1

    missing_meta = {
        PAIR_AC: {
            "family": "EQ_RANGE",
            "grow_reason": "seed_eq_plus_range",
            "grow_seed_key": A,
            "seed_key": A,
        }
    }
    missing_stats = gen._annotate_pair_fidelity(missing_meta)
    assert missing_stats["seed_family_missing_count"] == 1


def test_grow_seed_family_does_not_change_active_structural_pair_ranking():
    tuner = AdaSelect.__new__(AdaSelect)
    tuner._last_wdcg_score_map = {PAIR_AB: 10.0, PAIR_AC: 10.0}
    tuner._creation_cost = lambda _key: 0.0
    meta_map = {
        PAIR_AB: {
            "family": "EQ_RANGE",
            "grow_seed_family": "JOIN_EQ1",
            "seed_normalized_benefit": 0.1,
        },
        PAIR_AC: {
            "family": "EQ_RANGE",
            "seed_normalized_benefit": 0.9,
        },
    }

    ranked = tuner._rank_structural_pair_candidates([PAIR_AB, PAIR_AC], meta_map)

    assert tuner._structural_pair_type(PAIR_AB, meta_map) == "EQ_RANGE"
    assert tuner._diagnostic_structural_pair_type(PAIR_AB, meta_map) == "JOIN_RANGE"
    assert ranked == [PAIR_AC, PAIR_AB]


def test_pair_fate_classifier_distinguishes_caps_lane_eligibility_and_fire():
    tuner = AdaSelect.__new__(AdaSelect)
    tuner._last_wdcg_stats = {}
    tuner.replacement_overlay_enabled = True
    tuner._last_overlay_opportunity_pairs = {PAIR_AC, PAIR_BC, PAIR_AD}
    tuner._last_overlay_admitted_pairs = {PAIR_BC, PAIR_AD}
    tuner._last_overlay_fired_pairs = {PAIR_BC}
    tuner._wdcg_gen = type("FakeGen", (), {
        "last_pair_supply": {
            "prequery_width2": {PAIR_AB, PAIR_AC, PAIR_BC, PAIR_AD},
            "postquery_width2": {PAIR_AC, PAIR_BC, PAIR_AD},
            "dropped_perquery_width2": {PAIR_AB},
            "preround_width2": {PAIR_AC, PAIR_BC, PAIR_AD},
            "postround_width2": {PAIR_BC, PAIR_AD},
            "dropped_round_width2": {PAIR_AC},
        }
    })()

    tuner._record_pair_supply_diagnostics()

    assert tuner._last_pair_fate_map[PAIR_AB] == "dropped_perquery_cap"
    assert tuner._last_pair_fate_map[PAIR_AC] == "in_opportunity_blocked_by_lane"
    assert tuner._last_pair_fate_map[PAIR_BC] == "lane_admitted_fired"
    assert tuner._last_pair_fate_map[PAIR_AD] == "lane_admitted_blocked_by_eligibility"
    assert tuner._last_wdcg_stats["pair_fate_dropped_perquery_cap_count"] == 1
    assert tuner._last_wdcg_stats["pair_fate_in_opportunity_blocked_by_lane_count"] == 1
    assert tuner._last_wdcg_stats["pair_fate_lane_admitted_blocked_by_eligibility_count"] == 1
    assert tuner._last_wdcg_stats["pair_fate_lane_admitted_fired_count"] == 1


def test_pair_fate_distinguishes_overlay_disabled_from_eligibility_block():
    tuner = AdaSelect.__new__(AdaSelect)
    tuner._last_wdcg_stats = {}
    tuner.replacement_overlay_enabled = False
    tuner._last_overlay_opportunity_pairs = {PAIR_AB}
    tuner._last_overlay_admitted_pairs = {PAIR_AB}
    tuner._last_overlay_fired_pairs = set()
    tuner._wdcg_gen = type("FakeGen", (), {
        "last_pair_supply": {
            "prequery_width2": {PAIR_AB},
            "postquery_width2": {PAIR_AB},
            "dropped_perquery_width2": set(),
            "preround_width2": {PAIR_AB},
            "postround_width2": {PAIR_AB},
            "dropped_round_width2": set(),
        }
    })()

    tuner._record_pair_supply_diagnostics()

    assert tuner._last_pair_fate_map[PAIR_AB] == "lane_admitted_overlay_disabled"
    assert tuner._last_wdcg_stats["pair_fate_lane_admitted_overlay_disabled_count"] == 1
    assert tuner._last_wdcg_stats["pair_fate_lane_admitted_blocked_by_eligibility_count"] == 0


def test_overlay_pair_count_metrics_are_pair_level_not_round_only():
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.replacement_overlay_enabled = False
    tuner.max_num = 10
    tuner._last_wdcg_stats = {}
    tuner._last_structural_pair_candidate_set = {PAIR_AB, PAIR_AC}
    tuner._last_structural_pair_replacement_map = {
        PAIR_AB: {"left_prefix_single": A, "replacement_net_benefit": 0.0},
        PAIR_AC: {"left_prefix_single": A, "replacement_net_benefit": 0.0},
    }
    tuner._last_shadow_action_rows = []

    selected = tuner._record_replacement_overlay({A})

    assert selected == {A}
    assert tuner._last_wdcg_stats["overlay_opportunity_pair_count"] == 2
    assert tuner._last_wdcg_stats["overlay_lane_admitted_pair_count"] == 2
    assert tuner._last_wdcg_stats["overlay_fired_pair_count"] == 0
    assert tuner._last_wdcg_stats["overlay_blocked_by_eligibility_count"] == 0


def test_new_metrics_fields_are_serialized(tmp_path):
    path = tmp_path / "metrics.csv"
    recorder = MetricsRecorder(str(path), flush_each_row=False)
    recorder.record_round(
        round_id=0,
        old_conf=set(),
        new_conf=set(),
        width2_cap_dropped_round=3,
        pair_fate_lane_admitted_fired_count=1,
        overlay_blocked_by_lane_count=2,
    )
    recorder.close()

    with path.open(newline="", encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
    assert row["width2_cap_dropped_round"] == "3"
    assert row["pair_fate_lane_admitted_fired_count"] == "1"
    assert row["overlay_blocked_by_lane_count"] == "2"


def test_trace_records_pair_fate_and_grow_seed_metadata(tmp_path):
    path = tmp_path / "trace.csv"
    gen = type("FakeGen", (), {})()
    gen.enum = gen
    gen.last_meta = {
        PAIR_AB: {
            "family": "EQ_RANGE",
            "grow_seed_key": A,
            "seed_key": A,
            "grow_seed_family": "JOIN_EQ1",
            "grow_seed_family_set": ["JOIN_EQ1"],
            "grow_reason": "seed_eq_plus_range",
            "expected_structural_pair_type": "JOIN_RANGE",
            "pair_family_vs_grow_reason_mismatch": 1,
            "seed_family_missing": 1,
            "join_seed_downgraded": 1,
        }
    }
    tuner = type("FakeTuner", (), {})()
    tuner._wdcg_gen = gen
    tuner._last_wdcg_stats = {"pair_fate_lane_admitted_fired_count": 1}
    tuner._last_pair_fate_map = {PAIR_AB: "lane_admitted_fired"}
    tuner._last_final_conf = set()
    tuner._last_appearing_set = set()
    tuner._last_candidate_conf = set()
    tuner._last_evaluated_set = set()

    with TraceRecorder(path, flush_each_row=False) as tracer:
        tracer.record_round(0, old_conf=set(), new_conf=set(), tuner=tuner)

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["pair_fate"] == "lane_admitted_fired"
    assert rows[0]["structural_pair_type"] == "EQ_RANGE"
    assert rows[0]["diagnostic_structural_pair_type"] == "JOIN_RANGE"
    assert rows[0]["expected_structural_pair_type"] == "JOIN_RANGE"
    assert rows[0]["grow_seed_family"] == "JOIN_EQ1"
    assert rows[0]["pair_family_vs_grow_reason_mismatch"] == "1"
