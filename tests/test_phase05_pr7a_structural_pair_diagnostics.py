import csv
from pathlib import Path
from types import SimpleNamespace

from scripts.server.summarize_phase05 import _is_metrics_csv, _is_trace_csv, _summarize_width2_trace
from util.trace_recorder import TraceRecorder, covered_prefix_singles


def _read_trace(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class FakeTuner:
    def __init__(self, meta_map):
        self._last_appearing_set = {("lineitem", ("l_partkey", "l_shipdate"))}
        self._last_candidate_conf = set()
        self._last_final_conf = {("lineitem", ("l_partkey",))}
        self._last_evaluated_set = set()
        self._last_eval_order = [("lineitem", ("l_partkey", "l_shipdate"))]
        self._last_wdcg_score_map = {("lineitem", ("l_partkey", "l_shipdate")): 4.2}
        self._last_net_benefit_map = {("lineitem", ("l_partkey", "l_shipdate")): -0.1}
        self._last_obs_delta_map = {}
        self._last_obs_src_map = {}
        self._last_decision_stats = {"ratio": 1.0, "old_benefit": 0.2, "new_benefit": 0.2}
        self._last_deadzone_stats = {}
        self._last_wdcg_stats = {}
        self.columns_benefit = {("lineitem", ("l_partkey", "l_shipdate")): 0.0}
        self._wdcg_gen = SimpleNamespace(enum=SimpleNamespace(last_meta=meta_map))

    def _creation_cost(self, key):
        return 0.13 if len(key[1]) == 2 else 0.05


def test_covered_prefix_singles_detects_old_prefix_and_component_singles():
    pair = ("lineitem", ("l_partkey", "l_shipdate"))
    old_conf = {
        ("lineitem", ("l_partkey",)),
        ("lineitem", ("l_shipdate",)),
        ("orders", ("o_orderdate",)),
    }
    candidate_conf = set()

    assert covered_prefix_singles(pair, old_conf, candidate_conf) == (
        ("lineitem", ("l_partkey",)),
        ("lineitem", ("l_shipdate",)),
    )


def test_trace_records_width2_structural_pair_diagnostics_without_selecting_it(tmp_path):
    seed = ("lineitem", ("l_partkey",))
    pair = ("lineitem", ("l_partkey", "l_shipdate"))
    meta_map = {
        seed: {"family": "JOIN_EQ1", "score": 2.4},
        pair: {
            "family": "EQ_RANGE",
            "source": "AST",
            "score": 4.2,
            "seed_key": seed,
            "seed_benefit": 10.0,
            "seed_mature": True,
            "grow_reason": "seed_eq_plus_range",
        },
    }
    tuner = FakeTuner(meta_map)
    old_conf = {seed, ("lineitem", ("l_shipdate",))}
    new_conf = {seed}
    old_conf_before = set(old_conf)
    new_conf_before = set(new_conf)

    trace_path = tmp_path / "trace.csv"
    with TraceRecorder(trace_path) as tracer:
        tracer.record_round(
            round_id=2,
            old_conf=old_conf,
            new_conf=new_conf,
            evaluated_set=set(),
            tuner=tuner,
            algo_name="adaselect",
        )

    assert old_conf == old_conf_before
    assert new_conf == new_conf_before
    assert tuner._last_final_conf == {seed}

    pair_rows = [r for r in _read_trace(trace_path) if r["table"] == "lineitem" and r["cols"] == "l_partkey,l_shipdate"]
    assert len(pair_rows) == 1
    row = pair_rows[0]
    assert row["in_appearing"] == "1"
    assert row["in_eval"] == "0"
    assert row["in_new"] == "0"
    assert row["family"] == "EQ_RANGE"
    assert row["structural_pair_type"] == "JOIN_RANGE"
    assert row["covered_prefix_singles"] == "lineitem(l_partkey);lineitem(l_shipdate)"
    assert row["seed_benefit"] == "10.0"
    assert row["seed_mature"] == "True"
    assert row["grow_reason"] == "seed_eq_plus_range"


def test_trace_includes_width2_meta_candidate_even_when_not_appearing(tmp_path):
    pair = ("lineitem", ("l_partkey", "l_shipdate"))
    tuner = FakeTuner({pair: {"family": "EQ_RANGE", "score": 4.2}})
    tuner._last_appearing_set = set()
    tuner._last_eval_order = []

    trace_path = tmp_path / "trace.csv"
    with TraceRecorder(trace_path) as tracer:
        tracer.record_round(
            round_id=2,
            old_conf=set(),
            new_conf=set(),
            evaluated_set=set(),
            tuner=tuner,
            algo_name="adaselect",
        )

    rows = _read_trace(trace_path)
    assert any(r["table"] == "lineitem" and r["cols"] == "l_partkey,l_shipdate" for r in rows)


def test_width2_summary_counts_trace_funnel_rows():
    rows = [
        {
            "table": "lineitem",
            "cols": "l_partkey,l_shipdate",
            "in_appearing": "1",
            "in_eval": "0",
            "in_new": "0",
            "benefit": "0.0",
        },
        {
            "table": "orders",
            "cols": "o_custkey,o_orderdate",
            "in_appearing": "1",
            "in_eval": "1",
            "in_new": "1",
            "benefit": "3.0",
        },
    ]

    lines = _summarize_width2_trace(rows)

    assert "- width2_appeared_count: 2" in lines
    assert "- width2_evaluated_count: 1" in lines
    assert "- width2_selected_count: 1" in lines
    assert "- width2_with_zero_benefit_count: 1" in lines
    assert any("lineitem(l_partkey,l_shipdate)=1" in line for line in lines)


def test_summary_distinguishes_metrics_and_trace_csvs(tmp_path):
    metrics_path = tmp_path / "adaselect_tpchs_noisy.csv"
    trace_path = tmp_path / "adaselect_tpchs_noisy.trace.csv"
    metrics_path.write_text("round,candidate_count,evaluated_count\n0,2,1\n", encoding="utf-8")
    trace_path.write_text("round,table,cols,in_appearing,in_eval,in_new\n0,lineitem,\"l_partkey,l_shipdate\",1,0,0\n", encoding="utf-8")

    assert _is_metrics_csv(metrics_path)
    assert not _is_metrics_csv(trace_path)
    assert _is_trace_csv(trace_path)
