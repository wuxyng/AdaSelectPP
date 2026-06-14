import csv
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from adasel.ada_select import AdaSelect
from adasel.config_flags import (
    parse_target_pair_audit,
    resolve_pair_supply_ceiling_enabled,
    resolve_target_pair_audit,
)
from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from adaselect_pp.candidate_gen_v2.types import Candidate, QueryEvidence
from util.metrics_recorder import MetricsRecorder


A = ("t", ("a",))
B = ("t", ("b",))
C = ("t", ("c",))
PAIR_AB = ("t", ("a", "b"))
PAIR_AC = ("t", ("a", "c"))
PAIR_BC = ("t", ("b", "c"))
LINEITEM_PAIR = ("lineitem", ("l_partkey", "l_shipdate"))
ORDERS_PAIR = ("orders", ("o_custkey", "o_orderdate"))


def _candidate(key, family="EQ1", support=1, query_id=0):
    cand = Candidate(key=key, family=family, source="AST", confidence=0.9, roles=("test",))
    cand.query_ids = {int(query_id)}
    cand.template_ids = {f"q{int(query_id)}"}
    cand.support_count = support
    cand.score = MCIGCandidateGenerator.__new__(MCIGCandidateGenerator)._score(cand)
    return cand


def _generator(per_query_cap=2, per_table_cap=10, round_table_cap=10):
    gen = MCIGCandidateGenerator.__new__(MCIGCandidateGenerator)
    gen.per_query_cap = per_query_cap
    gen.per_table_cap = per_table_cap
    gen.round_table_cap = round_table_cap
    gen.probe_rounds = 0
    gen.extractor = type("FakeExtractor", (), {"sqlglot_available": True})()
    gen.vocab = type("FakeVocab", (), {"enabled": False, "path": "", "mapping": {}})()
    return gen


def _wire_fake_generation(gen, query_maps):
    evidences = [
        QueryEvidence(query_id=i, template_id=f"q{i}", sql=f"select {i}", parse_status="ast_ok")
        for i in range(len(query_maps))
    ]
    gen._extract_evidence = lambda _workload: (evidences, {"ast_ok": len(evidences), "fallback_regex": 0})
    gen._emit_single_probes = lambda evidence: dict(query_maps[evidence.query_id])
    gen._grow_width2 = lambda evidence, qmap, seed_states, rejected, grow_meta: {}
    gen._add_vacuum_rescue = lambda evidence, qmap: None


def test_default_query_ceiling_disabled_preserves_selection_order():
    gen = _generator(per_query_cap=2)
    out = {
        A: _candidate(A),
        B: _candidate(B),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", support=10),
    }

    legacy = gen._query_reduce(out)
    selected, diag = gen._query_reduce_with_diagnostics(out, pair_supply_ceiling_enabled=False)

    assert list(selected) == list(legacy)
    assert set(selected) == {A, B}
    assert diag["ceiling_added_width2"] == set()


def test_pair_supply_ceiling_keeps_width2_that_query_cap_would_drop():
    gen = _generator(per_query_cap=2)
    out = {
        A: _candidate(A),
        B: _candidate(B),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", support=10),
    }

    selected, diag = gen._query_reduce_with_diagnostics(out, pair_supply_ceiling_enabled=True)

    assert set(selected) == {A, B, PAIR_AB}
    assert diag["width2_dropped"] == {PAIR_AB}
    assert diag["ceiling_added_width2"] == {PAIR_AB}


def test_pair_supply_ceiling_keeps_width2_that_round_cap_would_drop():
    gen = _generator(round_table_cap=10)
    merged = {
        A: _candidate(A, "EQ1"),
        B: _candidate(B, "JOIN_EQ1"),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", support=20),
    }

    selected, diag = gen._round_select_with_diagnostics(
        merged,
        topk=2,
        pair_supply_ceiling_enabled=True,
    )

    assert [cand.key for cand in selected] == [A, B, PAIR_AB]
    assert diag["width2_dropped"] == {PAIR_AB}
    assert diag["ceiling_added_width2"] == {PAIR_AB}


def test_pair_supply_ceiling_does_not_generate_absent_width2():
    gen = _generator(per_query_cap=10)
    out = {A: _candidate(A), B: _candidate(B)}

    selected, diag = gen._query_reduce_with_diagnostics(out, pair_supply_ceiling_enabled=True)

    assert set(selected) == {A, B}
    assert diag["width2_before"] == set()
    assert diag["ceiling_added_width2"] == set()


def test_ceiling_candidate_delta_ignores_cross_query_non_delta():
    gen = _generator(per_query_cap=2)
    _wire_fake_generation(gen, [
        {
            A: _candidate(A, query_id=0),
            B: _candidate(B, query_id=0),
            PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", support=5, query_id=0),
        },
        {
            PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", support=5, query_id=1),
        },
    ])

    res = gen.generate(
        ["q0", "q1"],
        topk=10,
        workload_count=2,
        pair_supply_ceiling_enabled=True,
        target_pair_audit={PAIR_AB},
    )

    assert res.stats["pair_supply_ceiling_width2_added_perquery"] > 0
    assert PAIR_AB in res.topk_set
    assert res.stats["pair_supply_ceiling_candidate_count_delta"] == 0
    assert res.stats["pair_supply_ceiling_target_pairs_recovered"] == 0
    assert res.stats["pair_supply_ceiling_examples"] == ""


def test_ceiling_candidate_delta_counts_true_target_recovery():
    gen = _generator(per_query_cap=2)
    _wire_fake_generation(gen, [
        {
            A: _candidate(A, query_id=0),
            B: _candidate(B, query_id=0),
            PAIR_AC: _candidate(PAIR_AC, "EQ_RANGE", support=5, query_id=0),
        },
    ])

    res = gen.generate(
        ["q0"],
        topk=10,
        workload_count=2,
        pair_supply_ceiling_enabled=True,
        target_pair_audit={PAIR_AC},
    )

    assert PAIR_AC in res.topk_set
    assert res.stats["pair_supply_ceiling_candidate_count_delta"] == 1
    assert res.stats["pair_supply_ceiling_target_pairs_recovered"] == 1
    assert res.stats["pair_supply_ceiling_examples"] == "t(a,c)"


def test_target_pair_audit_parser_and_resolution():
    parsed = parse_target_pair_audit("orders(o_custkey,o_orderdate); lineitem(l_partkey,l_shipdate)")

    assert parsed == {ORDERS_PAIR, LINEITEM_PAIR}
    assert resolve_target_pair_audit(None, "lineitem(l_partkey,l_shipdate)", "", default="") == "lineitem(l_partkey,l_shipdate)"
    assert resolve_target_pair_audit("orders(o_custkey,o_orderdate)", "lineitem(l_partkey,l_shipdate)", "", default="") == "orders(o_custkey,o_orderdate)"


def test_pair_supply_ceiling_precedence_cli_env_config_default():
    assert resolve_pair_supply_ceiling_enabled(0, "1", True, default=False) is False
    assert resolve_pair_supply_ceiling_enabled(1, "0", False, default=False) is True
    assert resolve_pair_supply_ceiling_enabled(None, "1", False, default=False) is True
    assert resolve_pair_supply_ceiling_enabled(None, None, True, default=False) is True
    assert resolve_pair_supply_ceiling_enabled(None, None, None, default=False) is False


def test_target_pair_audit_records_coverage_and_fate():
    tuner = AdaSelect.__new__(AdaSelect)
    tuner._last_wdcg_stats = {}
    tuner.target_pair_audit = {PAIR_AB, PAIR_AC, PAIR_BC}
    tuner.replacement_overlay_enabled = False
    tuner._last_overlay_opportunity_pairs = {PAIR_AB}
    tuner._last_overlay_admitted_pairs = {PAIR_AB}
    tuner._last_overlay_fired_pairs = set()
    tuner._last_final_conf = {PAIR_BC}
    tuner._last_candidate_conf = {PAIR_AC}
    tuner._wdcg_gen = type("FakeGen", (), {
        "last_pair_supply": {
            "prequery_width2": {PAIR_AB, PAIR_AC},
            "postquery_width2": {PAIR_AB},
            "dropped_perquery_width2": {PAIR_AC},
            "preround_width2": {PAIR_AB},
            "postround_width2": set(),
            "dropped_round_width2": {PAIR_AB},
        }
    })()

    tuner._record_pair_supply_diagnostics(selected_conf={PAIR_AC}, final_conf={PAIR_BC})

    assert tuner._last_wdcg_stats["target_pair_count"] == 3
    assert tuner._last_wdcg_stats["target_pair_prequery_coverage_count"] == 2
    assert tuner._last_wdcg_stats["target_pair_postquery_coverage_count"] == 1
    assert tuner._last_wdcg_stats["target_pair_preround_coverage_count"] == 1
    assert tuner._last_wdcg_stats["target_pair_postround_coverage_count"] == 0
    assert tuner._last_wdcg_stats["target_pair_lane_admitted_count"] == 1
    assert tuner._last_wdcg_stats["target_pair_selected_count"] == 1
    assert tuner._last_wdcg_stats["target_pair_final_count"] == 1
    assert tuner._last_pair_fate_map[PAIR_AB] == "lane_admitted_overlay_disabled"
    assert "t(b,c)" in tuner._last_wdcg_stats["target_pair_missing_examples"]


def test_new_metrics_fields_are_serialized(tmp_path):
    path = tmp_path / "metrics.csv"
    recorder = MetricsRecorder(str(path), flush_each_row=False)
    recorder.record_round(
        round_id=0,
        old_conf=set(),
        new_conf=set(),
        pair_supply_ceiling_enabled=1,
        pair_supply_ceiling_width2_added_round=2,
        target_pair_final_count=1,
        target_pair_fate_summary="t(a,b)=generated_not_in_overlay_opportunity",
    )
    recorder.close()

    with path.open(newline="", encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
    assert row["pair_supply_ceiling_enabled"] == "1"
    assert row["pair_supply_ceiling_width2_added_round"] == "2"
    assert row["target_pair_final_count"] == "1"
    assert row["target_pair_fate_summary"] == "t(a,b)=generated_not_in_overlay_opportunity"


def test_legacy_param_runner_dry_run_exposes_ceiling_and_target_pair_args():
    if os.name == "nt":
        pytest.skip("Windows checkout uses CRLF shell scripts; runner DRY_RUN is validated on Linux")
    if shutil.which("bash") is None:
        pytest.skip("bash is unavailable")
    run_dir = Path(f"runs/pr14a_dry_run_{os.getpid()}")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    env = os.environ.copy()
    env.update({
        "DRY_RUN": "1",
        "CASE_FILTER": "tpchs:random",
        "PAIR_SUPPLY_CEILING": "1",
        "TARGET_PAIR_AUDIT": "lineitem(l_partkey,l_shipdate)",
        "RUN_DIR": str(run_dir),
    })

    try:
        proc = subprocess.run(
            ["bash", "scripts/server/run_phase05_legacy_params.sh"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        assert proc.returncode == 0, proc.stderr
        metadata = (run_dir / "tpchs_random" / "metadata.env").read_text(encoding="utf-8")
        command = (run_dir / "tpchs_random" / "command.txt").read_text(encoding="utf-8")
        assert "pair_supply_ceiling_enabled=1" in metadata
        assert "target_pair_audit=lineitem(l_partkey,l_shipdate)" in metadata
        assert "--pair_supply_ceiling_enabled 1" in command
        assert "--target_pair_audit lineitem(l_partkey,l_shipdate)" in command
    finally:
        if run_dir.exists():
            shutil.rmtree(run_dir)
