import pytest

from adasel.config_flags import resolve_candidate_generation_mode
from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from adaselect_pp.candidate_gen_v2.types import Candidate, QueryEvidence


A = ("t", ("a",))
B = ("t", ("b",))
C = ("t", ("c",))
PAIR_AB = ("t", ("a", "b"))
PAIR_BA = ("t", ("b", "a"))
PAIR_AC = ("t", ("a", "c"))


def _generator(round_table_cap=10):
    gen = MCIGCandidateGenerator.__new__(MCIGCandidateGenerator)
    gen.max_width = 2
    gen.max_num = 10
    gen.per_query_cap = 10
    gen.per_table_cap = 10
    gen.round_table_cap = round_table_cap
    gen.probe_rounds = 0
    gen.extractor = type("FakeExtractor", (), {"sqlglot_available": True})()
    gen.vocab = type("FakeVocab", (), {"enabled": False, "path": "", "mapping": {}})()
    gen.db = type("ExplodingDB", (), {"exec_fetchall": lambda self, sql: (_ for _ in ()).throw(AssertionError("db call"))})()
    return gen


def _candidate(key, family="EQ1", score=None):
    cand = Candidate(key=key, family=family, source="AST", confidence=0.9, roles=("test",))
    cand.query_ids = {0}
    cand.template_ids = {"q0"}
    cand.support_count = 1
    cand.score = float(score) if score is not None else MCIGCandidateGenerator.__new__(MCIGCandidateGenerator)._score(cand)
    return cand


def _wire_fake_generation(gen, qmap, grow_meta=None):
    evidence = QueryEvidence(query_id=0, template_id="q0", sql="select 1", parse_status="ast_ok")
    grow_meta = dict(grow_meta or {})
    gen._extract_evidence = lambda _workload: ([evidence], {"ast_ok": 1, "fallback_regex": 0})
    gen._emit_single_probes = lambda _evidence: dict(qmap)
    gen._grow_width2 = lambda _evidence, _qmap, _seed_states, _rejected, out_meta: out_meta.update(grow_meta) or {}
    gen._add_vacuum_rescue = lambda _evidence, _qmap: None


def test_candidate_generation_mode_resolution_precedence():
    assert resolve_candidate_generation_mode("probe_grow_fair", "probe_grow", "probe_grow") == "probe_grow_fair"
    assert resolve_candidate_generation_mode(None, "probe_grow_fair", "probe_grow") == "probe_grow_fair"
    assert resolve_candidate_generation_mode(None, None, "fair") == "probe_grow_fair"
    assert resolve_candidate_generation_mode(None, None, None) == "probe_grow"
    with pytest.raises(ValueError):
        resolve_candidate_generation_mode("wide2_enum", None, None)


def test_default_probe_grow_behavior_unchanged_when_mode_disabled():
    gen = _generator(round_table_cap=2)
    _wire_fake_generation(gen, {
        A: _candidate(A, score=10),
        B: _candidate(B, score=9),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
    })

    res = gen.generate(["q0"], topk=2, workload_count=2, candidate_generation_mode="probe_grow")

    assert set(res.topk_set) == {A, B}
    assert res.stats["candidate_generation_mode"] == "probe_grow"
    assert res.stats["pair_supply_fairness_enabled"] == 0
    assert res.stats["cg_width2_fairness_added_count"] == 0


def test_probe_grow_fair_mode_recovers_width2_under_cap_and_keeps_width_scope():
    gen = _generator(round_table_cap=2)
    _wire_fake_generation(gen, {
        A: _candidate(A, score=10),
        B: _candidate(B, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
    })

    res = gen.generate(
        ["q0"],
        topk=2,
        workload_count=2,
        candidate_generation_mode="probe_grow_fair",
        pair_supply_per_table_width2_reserve=1,
        pair_supply_round_width2_reserve=4,
        target_pair_audit={PAIR_AB},
    )

    assert gen.max_width == 2
    assert PAIR_AB in set(res.topk_set)
    assert len(res.topk_set) == 2
    assert sum(1 for key in res.topk_set if len(key[1]) == 1) == 1
    assert res.stats["candidate_generation_mode"] == "probe_grow_fair"
    assert res.stats["pair_supply_fairness_enabled"] == 1
    assert res.stats["cg_width2_pre_cap_count"] == 1
    assert res.stats["cg_width2_post_cap_count"] == 1
    assert res.stats["cg_width2_dropped_round_count"] == 1
    assert res.stats["cg_width2_fairness_added_count"] == 1
    assert res.stats["cg_width2_fairness_added_pairs"] == "t(a,b)"
    assert res.stats["cg_target_pair_postround_coverage_count"] == 1
    assert res.stats["cg_candidate_budget_delta"] == 1


def test_probe_grow_fair_table_cap_remains_bounded():
    gen = _generator(round_table_cap=2)
    _wire_fake_generation(gen, {
        A: _candidate(A, score=10),
        B: _candidate(B, score=2),
        C: _candidate(C, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
        PAIR_AC: _candidate(PAIR_AC, "EQ_RANGE", score=90),
    })

    res = gen.generate(
        ["q0"],
        topk=2,
        workload_count=2,
        candidate_generation_mode="probe_grow_fair",
        pair_supply_per_table_width2_reserve=2,
        pair_supply_round_width2_reserve=4,
    )

    assert len(res.topk_set) == 2
    assert sum(1 for key in res.topk_set if key[0] == "t") == 2


def test_probe_grow_fair_columnset_diversity_uses_one_per_unordered_pair():
    gen = _generator(round_table_cap=2)
    _wire_fake_generation(gen, {
        A: _candidate(A, score=10),
        B: _candidate(B, score=1),
        PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100),
        PAIR_BA: _candidate(PAIR_BA, "EQ_RANGE", score=90),
    })

    res = gen.generate(
        ["q0"],
        topk=2,
        workload_count=2,
        candidate_generation_mode="probe_grow_fair",
        pair_supply_per_table_width2_reserve=2,
        pair_supply_round_width2_reserve=4,
    )

    width2 = {key for key in res.topk_set if len(key[1]) == 2}
    assert width2 == {PAIR_AB}
    assert res.stats["pair_supply_fairness_columnset_dedup_count"] == 1


def test_probe_grow_fair_uses_diagnostic_structural_type_ranking():
    gen = _generator(round_table_cap=2)
    _wire_fake_generation(
        gen,
        {
            A: _candidate(A, score=10),
            B: _candidate(B, score=1),
            PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=10),
            PAIR_AC: _candidate(PAIR_AC, "EQ_EQ", score=100),
        },
        grow_meta={
            PAIR_AB: {
                "grow_seed_family": "JOIN_EQ1",
                "grow_seed_family_set": ["JOIN_EQ1"],
                "grow_reason": "seed_eq_plus_range",
                "expected_structural_pair_type": "JOIN_RANGE",
            }
        },
    )

    res = gen.generate(
        ["q0"],
        topk=2,
        workload_count=2,
        candidate_generation_mode="probe_grow_fair",
        pair_supply_per_table_width2_reserve=1,
        pair_supply_round_width2_reserve=1,
    )

    assert PAIR_AB in res.topk_set
    assert PAIR_AC not in res.topk_set


def test_probe_grow_fair_and_ceiling_are_mutually_exclusive():
    gen = _generator()
    _wire_fake_generation(gen, {PAIR_AB: _candidate(PAIR_AB, "EQ_RANGE", score=100)})

    with pytest.raises(ValueError, match="mutually exclusive"):
        gen.generate(
            ["q0"],
            topk=2,
            workload_count=2,
            candidate_generation_mode="probe_grow_fair",
            pair_supply_ceiling_enabled=True,
        )
