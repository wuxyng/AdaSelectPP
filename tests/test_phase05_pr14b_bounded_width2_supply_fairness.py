import os
import shutil
import subprocess
from pathlib import Path

import pytest

from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from adaselect_pp.candidate_gen_v2.types import Candidate, QueryEvidence


A = ("t", ("a",))
B = ("t", ("b",))
C = ("t", ("c",))
D = ("t", ("d",))
PAIR_AB = ("t", ("a", "b"))
PAIR_BA = ("t", ("b", "a"))
PAIR_AC = ("t", ("a", "c"))
PAIR_AD = ("t", ("a", "d"))


def _generator(per_query_cap=10, per_table_cap=10, round_table_cap=10):
    gen = MCIGCandidateGenerator.__new__(MCIGCandidateGenerator)
    gen.per_query_cap = per_query_cap
    gen.per_table_cap = per_table_cap
    gen.round_table_cap = round_table_cap
    gen.probe_rounds = 0
    gen.extractor = type("FakeExtractor", (), {"sqlglot_available": True})()
    gen.vocab = type("FakeVocab", (), {"enabled": False, "path": "", "mapping": {}})()
    return gen


def _candidate(key, family="EQ1", score=None, support=1, query_id=0):
    cand = Candidate(key=key, family=family, source="AST", confidence=0.9, roles=("test",))
    cand.query_ids = {int(query_id)}
    cand.template_ids = {f"q{int(query_id)}"}
    cand.support_count = support
    if score is None:
        cand.score = MCIGCandidateGenerator.__new__(MCIGCandidateGenerator)._score(cand)
    else:
        cand.score = float(score)
    return cand


def _wire_fake_generation(gen, query_maps, grow_meta=None):
    evidences = [
        QueryEvidence(query_id=i, template_id=f"q{i}", sql=f"select {i}", parse_status="ast_ok")
        for i in range(len(query_maps))
    ]
    grow_meta = dict(grow_meta or {})
    gen._extract_evidence = lambda _workload: (evidences, {"ast_ok": len(evidences), "fallback_regex": 0})
    gen._emit_single_probes = lambda evidence: dict(query_maps[evidence.query_id])
    gen._grow_width2 = lambda evidence, qmap, seed_states, rejected, out_meta: out_meta.update(grow_meta) or {}
    gen._add_vacuum_rescue = lambda evidence, qmap: None


def _keys(selected):
    return [cand.key for cand in selected]


def test_default_disabled_round_selection_identical():
    gen = _generator(round_table_cap=2)
    merged = {
        A: _candidate(A, score=10),
        B: _candidate(B, score=9),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
    }

    baseline, _ = gen._round_select_with_diagnostics(merged, topk=2)
    disabled, diag = gen._round_select_with_diagnostics(
        merged,
        topk=2,
        pair_supply_fairness_enabled=False,
        pair_supply_per_table_width2_reserve=1,
        pair_supply_round_width2_reserve=4,
    )

    assert _keys(disabled) == _keys(baseline)
    assert diag["fairness_added_width2"] == set()
    assert diag["fairness_displaced_width1"] == set()


def test_enabled_reserve1_displaces_same_table_width1_without_exceeding_caps():
    gen = _generator(round_table_cap=2)
    merged = {
        A: _candidate(A, score=10),
        B: _candidate(B, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
    }

    selected, diag = gen._round_select_with_diagnostics(
        merged,
        topk=2,
        pair_supply_fairness_enabled=True,
        pair_supply_per_table_width2_reserve=1,
        pair_supply_round_width2_reserve=4,
    )

    assert set(_keys(selected)) == {A, PAIR_AB}
    assert len(selected) == 2
    assert diag["fairness_added_width2"] == {PAIR_AB}
    assert diag["fairness_displaced_width1"] == {B}


def test_enabled_reserve2_can_keep_two_width2_for_same_table():
    gen = _generator(round_table_cap=3)
    merged = {
        A: _candidate(A, score=10),
        B: _candidate(B, score=2),
        C: _candidate(C, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
        PAIR_AC: _candidate(PAIR_AC, "EQ_RANGE", score=90),
    }

    selected, diag = gen._round_select_with_diagnostics(
        merged,
        topk=3,
        pair_supply_fairness_enabled=True,
        pair_supply_per_table_width2_reserve=2,
        pair_supply_round_width2_reserve=4,
    )

    assert {PAIR_AB, PAIR_AC}.issubset(set(_keys(selected)))
    assert len(selected) == 3
    assert len([key for key in _keys(selected) if len(key[1]) == 2]) == 2
    assert diag["fairness_added_width2"] == {PAIR_AB, PAIR_AC}
    assert diag["fairness_displaced_width1"] == {B, C}


def test_columnset_diversity_keeps_only_one_per_unordered_pair():
    gen = _generator(round_table_cap=2)
    merged = {
        A: _candidate(A, score=10),
        B: _candidate(B, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
        PAIR_BA: _candidate(PAIR_BA, "EQ_RANGE", score=90),
    }

    selected, diag = gen._round_select_with_diagnostics(
        merged,
        topk=2,
        pair_supply_fairness_enabled=True,
        pair_supply_per_table_width2_reserve=2,
        pair_supply_round_width2_reserve=4,
    )

    width2 = [key for key in _keys(selected) if len(key[1]) == 2]
    assert len(width2) == 1
    assert set(width2) == {PAIR_AB}
    assert diag["fairness_columnset_dedup_count"] == 1


def test_ranking_uses_expected_structural_type_not_raw_downgraded_family():
    gen = _generator(round_table_cap=2)
    merged = {
        A: _candidate(A, score=10),
        B: _candidate(B, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=10),
        PAIR_AC: _candidate(PAIR_AC, "EQ_EQ", score=100),
    }
    grow_meta = {
        PAIR_AB: {
            "grow_seed_family": "JOIN_EQ1",
            "grow_seed_family_set": ["JOIN_EQ1"],
            "grow_reason": "seed_eq_plus_range",
            "expected_structural_pair_type": "JOIN_RANGE",
        }
    }

    selected, diag = gen._round_select_with_diagnostics(
        merged,
        topk=2,
        pair_supply_fairness_enabled=True,
        pair_supply_per_table_width2_reserve=1,
        pair_supply_round_width2_reserve=1,
        grow_meta=grow_meta,
    )

    assert PAIR_AB in set(_keys(selected))
    assert PAIR_AC not in set(_keys(selected))
    assert diag["fairness_added_width2"] == {PAIR_AB}


def test_no_same_table_width1_to_displace_records_block_reason():
    gen = _generator(round_table_cap=1)
    merged = {
        PAIR_AC: _candidate(PAIR_AC, "EQ_RANGE", score=100),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=90),
    }

    selected, diag = gen._round_select_with_diagnostics(
        merged,
        topk=1,
        pair_supply_fairness_enabled=True,
        pair_supply_per_table_width2_reserve=2,
        pair_supply_round_width2_reserve=4,
    )

    assert _keys(selected) == [PAIR_AC]
    assert diag["fairness_added_width2"] == set()
    assert diag["fairness_block_reasons"]["no_same_table_width1"] == 1


def test_fairness_true_delta_ignores_pair_already_in_normal_topk():
    gen = _generator()
    _wire_fake_generation(gen, [{PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100)}])

    res = gen.generate(
        ["q0"],
        topk=10,
        workload_count=2,
        pair_supply_fairness_enabled=True,
        target_pair_audit={PAIR_AB},
    )

    assert PAIR_AB in res.topk_set
    assert res.stats["pair_supply_fairness_candidate_count_delta"] == 0
    assert res.stats["pair_supply_fairness_target_pairs_recovered"] == 0


def test_fairness_true_delta_counts_new_width2_recovery():
    gen = _generator(round_table_cap=2)
    _wire_fake_generation(gen, [{
        A: _candidate(A, score=10),
        B: _candidate(B, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
    }])

    res = gen.generate(
        ["q0"],
        topk=2,
        workload_count=2,
        pair_supply_fairness_enabled=True,
        pair_supply_per_table_width2_reserve=1,
        target_pair_audit={PAIR_AB},
    )

    assert PAIR_AB in res.topk_set
    assert res.stats["pair_supply_fairness_candidate_count_delta"] == 1
    assert res.stats["pair_supply_fairness_target_pairs_recovered"] == 1
    assert res.stats["pair_supply_fairness_target_pairs_recovered_examples"] == "t(a,b)"


def test_fairness_does_not_call_database_or_cost_paths():
    gen = _generator(round_table_cap=2)
    gen.db = type("ExplodingDB", (), {"exec_fetchall": lambda self, sql: (_ for _ in ()).throw(AssertionError("db call"))})()
    merged = {
        A: _candidate(A, score=10),
        B: _candidate(B, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
    }

    selected, _diag = gen._round_select_with_diagnostics(
        merged,
        topk=2,
        pair_supply_fairness_enabled=True,
        pair_supply_per_table_width2_reserve=1,
    )

    assert PAIR_AB in set(_keys(selected))


def test_ceiling_and_fairness_fail_fast():
    gen = _generator()

    with pytest.raises(ValueError, match="mutually exclusive"):
        gen.generate(
            ["q0"],
            topk=2,
            pair_supply_ceiling_enabled=True,
            pair_supply_fairness_enabled=True,
        )


def test_legacy_param_runner_dry_run_exposes_fairness_args():
    if os.name == "nt":
        pytest.skip("Windows checkout uses CRLF shell scripts; runner DRY_RUN is validated on Linux")
    if shutil.which("bash") is None:
        pytest.skip("bash is unavailable")
    run_dir = Path(f"runs/pr14b_dry_run_{os.getpid()}")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    env = os.environ.copy()
    env.update({
        "DRY_RUN": "1",
        "CASE_FILTER": "tpchs:random",
        "PAIR_SUPPLY_FAIRNESS": "1",
        "PAIR_SUPPLY_PER_TABLE_WIDTH2_RESERVE": "2",
        "PAIR_SUPPLY_ROUND_WIDTH2_RESERVE": "5",
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
        assert "pair_supply_fairness_enabled=1" in metadata
        assert "pair_supply_per_table_width2_reserve=2" in metadata
        assert "pair_supply_round_width2_reserve=5" in metadata
        assert "--pair_supply_fairness_enabled 1" in command
        assert "--pair_supply_per_table_width2_reserve 2" in command
        assert "--pair_supply_round_width2_reserve 5" in command
    finally:
        if run_dir.exists():
            shutil.rmtree(run_dir)
