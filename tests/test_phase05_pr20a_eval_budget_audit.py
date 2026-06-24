import csv
from pathlib import Path
from types import SimpleNamespace

from adasel.ada_select import AdaSelect
from util.trace_recorder import TraceRecorder


SINGLE_A = ("t", ("a",))
SINGLE_C = ("t", ("c",))
PAIR_AB = ("t", ("a", "b"))
PAIR_CD = ("t", ("c", "d"))


def _read_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_eval_budget_audit_stats_are_recorded_without_policy_effects():
    tuner = object.__new__(AdaSelect)
    tuner.workload_count = 3
    tuner.ratio = 0.25
    tuner.max_num = 10
    tuner.candidate_topk_factor = 4
    tuner.candidate_topk_min_extra = 6
    tuner._last_wdcg_stats = {"candidate_count_raw": 12, "candidate_topk": 46}
    tuner._last_evaluated_set = {SINGLE_A, PAIR_AB}

    tuner._record_eval_budget_audit(
        {SINGLE_A, SINGLE_C, PAIR_AB, PAIR_CD},
        budget=1,
        evaluated_set=tuner._last_evaluated_set,
    )

    stats = tuner._last_wdcg_stats
    assert stats["candidate_count_raw"] == 12
    assert stats["appearing_count"] == 4
    assert stats["candidate_topk"] == 46
    assert stats["optimizer_ratio"] == 0.25
    assert stats["eval_budget_formula"] == "max(1,int(optimizer_ratio*appearing_count))"
    assert stats["eval_budget"] == 1
    assert stats["evaluated_count"] == 2
    assert stats["budgeted_out_count"] == 2
    assert stats["width1_appearing_count"] == 2
    assert stats["width2_appearing_count"] == 2
    assert stats["width1_evaluated_count"] == 1
    assert stats["width2_evaluated_count"] == 1
    assert stats["width2_eval_coverage_ratio"] == 0.5


def test_trace_emits_eval_budget_audit_fields(tmp_path):
    path = tmp_path / "trace.csv"
    wdcg_stats = {
        "candidate_count_raw": 12,
        "appearing_count": 4,
        "candidate_topk": 46,
        "optimizer_ratio": 0.25,
        "eval_budget_formula": "max(1,int(optimizer_ratio*appearing_count))",
        "eval_budget": 1,
        "evaluated_count": 2,
        "budgeted_out_count": 2,
        "width1_appearing_count": 2,
        "width2_appearing_count": 2,
        "width1_evaluated_count": 1,
        "width2_evaluated_count": 1,
        "width2_eval_coverage_ratio": 0.5,
        "structural_pair_eval_budgeted_out_count": 1,
        "fairness_eval_lane_budgeted_out_count": 1,
        "materialization_gap_eval_gap_count": 1,
    }
    tuner = SimpleNamespace(
        _last_appearing_set={SINGLE_A, SINGLE_C, PAIR_AB, PAIR_CD},
        _last_candidate_conf=set(),
        _last_final_conf=set(),
        _last_evaluated_set={SINGLE_A, PAIR_AB},
        _last_eval_order=[SINGLE_A, PAIR_AB],
        _last_wdcg_score_map={},
        _last_net_benefit_map={},
        _last_obs_delta_map={},
        _last_obs_src_map={},
        _last_decision_stats={},
        _last_deadzone_stats={},
        _last_wdcg_stats=wdcg_stats,
        columns_benefit={},
        _wdcg_gen=SimpleNamespace(enum=SimpleNamespace(last_meta={})),
    )
    tuner._creation_cost = lambda _key: 0.0

    with TraceRecorder(path, flush_each_row=False) as tracer:
        tracer.record_round(
            0,
            old_conf=set(),
            new_conf=set(),
            evaluated_set=tuner._last_evaluated_set,
            tuner=tuner,
            algo_name="adaselect",
        )

    rows = _read_rows(path)
    assert rows
    row = rows[0]
    assert row["candidate_count_raw"] == "12"
    assert row["appearing_count"] == "4"
    assert row["candidate_topk"] == "46"
    assert row["optimizer_ratio"] == "0.25"
    assert row["eval_budget_formula"] == "max(1,int(optimizer_ratio*appearing_count))"
    assert row["eval_budget"] == "1"
    assert row["evaluated_count"] == "2"
    assert row["budgeted_out_count"] == "2"
    assert row["width1_appearing_count"] == "2"
    assert row["width2_appearing_count"] == "2"
    assert row["width1_evaluated_count"] == "1"
    assert row["width2_evaluated_count"] == "1"
    assert row["width2_eval_coverage_ratio"] == "0.5"
    assert row["structural_pair_eval_budgeted_out_count"] == "1"
    assert row["fairness_eval_lane_budgeted_out_count"] == "1"
    assert row["materialization_gap_eval_gap"] == "1"
