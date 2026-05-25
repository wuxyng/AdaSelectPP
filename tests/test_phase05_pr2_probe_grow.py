from pathlib import Path
from collections import Counter

from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from adaselect_pp.candidate_gen_v2.types import QueryEvidence


class FakeDB:
    def __init__(self):
        self._cols = {"t": ["a", "b", "c"], "u": ["a", "b"]}

    def get_tables(self):
        return list(self._cols)

    def get_columns(self, table):
        return list(self._cols[str(table)])

    def exec_fetchall(self, _sql):
        return []


def _write_whitelist(tmp_path: Path) -> None:
    (tmp_path / "txt").mkdir()
    (tmp_path / "txt" / "fake_indexable_columns.txt").write_text(
        "t a\nt b\nt c\nu a\nu b\n",
        encoding="utf-8",
    )


def _gen(tmp_path, monkeypatch) -> MCIGCandidateGenerator:
    monkeypatch.chdir(tmp_path)
    _write_whitelist(tmp_path)
    return MCIGCandidateGenerator("fake", FakeDB(), max_width=2, max_num=50, per_query_cap=20, per_table_cap=20, round_table_cap=20)


def _positive_seed_kwargs(key=("t", ("a",))):
    return {
        "seed_benefit": {key: 10.0},
        "seed_seen_count": {key: 1},
        "seed_positive_count": {key: 1},
        "seed_last_obs_src": {key: "OK"},
        "seed_first_seen_round": {key: 0},
        "seed_last_seen_round": {key: 1},
        "seed_seen_rounds": {key: {0, 1}},
        "seed_normalized_benefit": {key: 1.0},
    }


def _ast_evidence(*, has_or=False):
    return QueryEvidence(
        query_id=0,
        template_id="q1",
        sql="select * from t where a = 1 and b = 2 and c > 3",
        tables={"t"},
        filter_eq={"t": ["a", "b"]},
        filter_rng={"t": ["c"]},
        parse_status="ast_ok",
        has_or=has_or,
    )


def _generate_with_evidence(gen, evidence, **kwargs):
    gen.extractor.extract_line = lambda _line, _qid: evidence
    return gen.generate(["q1\t" + evidence.sql], **kwargs)


def test_round_0_and_1_emit_only_width1(tmp_path, monkeypatch):
    gen = _gen(tmp_path, monkeypatch)
    workload = ["q1\tselect * from t where a = 1 and b = 2 and c > 3"]

    r0 = gen.generate(workload, workload_count=0, **_positive_seed_kwargs())
    r1 = gen.generate(workload, workload_count=1, **_positive_seed_kwargs())

    assert all(len(key[1]) == 1 for key in r0.topk_set)
    assert all(len(key[1]) == 1 for key in r1.topk_set)
    assert r0.stats["gen_mode"] == "probe"
    assert r1.stats["gen_mode"] == "probe"


def test_round_2_width2_only_from_positive_evaluated_seed(tmp_path, monkeypatch):
    gen = _gen(tmp_path, monkeypatch)
    evidence = _ast_evidence()

    no_seed = _generate_with_evidence(gen, evidence, workload_count=2)
    with_seed = _generate_with_evidence(gen, evidence, workload_count=2, **_positive_seed_kwargs())

    assert all(len(key[1]) == 1 for key in no_seed.topk_set)
    assert ("t", ("a", "b")) in with_seed.topk_set
    assert ("t", ("a", "c")) in with_seed.topk_set
    assert ("t", ("b", "a")) not in with_seed.topk_set
    assert with_seed.meta_map[("t", ("a", "b"))]["seed_key"] == ("t", ("a",))
    assert with_seed.meta_map[("t", ("a", "b"))]["seed_evaluated_count"] == 1
    assert with_seed.meta_map[("t", ("a", "b"))]["seed_positive_count"] == 1
    assert with_seed.meta_map[("t", ("a", "b"))]["grow_reason"] == "seed_eq_plus_eq"
    assert with_seed.meta_map[("t", ("a", "c"))]["grow_reason"] == "seed_eq_plus_range"


def test_singles_survive_before_width2_under_caps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_whitelist(tmp_path)
    gen = MCIGCandidateGenerator("fake", FakeDB(), max_width=2, max_num=50, per_query_cap=4, per_table_cap=4, round_table_cap=4)
    evidence = _ast_evidence()

    res = _generate_with_evidence(
        gen,
        evidence,
        workload_count=2,
        **_positive_seed_kwargs(),
    )

    assert {("t", ("a",)), ("t", ("b",)), ("t", ("c",))}.issubset(res.topk_set)
    width2 = [key for key in res.topk_set if len(key[1]) == 2]
    assert len(width2) <= 1


def test_regex_fallback_does_not_grow_width2(tmp_path, monkeypatch):
    gen = _gen(tmp_path, monkeypatch)
    evidence = QueryEvidence(
        query_id=0,
        template_id="q1",
        sql="select * from t where a = 1 and b = 2",
        tables={"t"},
        filter_eq={"t": ["a", "b"]},
        parse_status="fallback_regex",
    )
    gen.extractor.extract_line = lambda _line, _qid: evidence

    res = gen.generate(["q1\tselect * from t where a = 1 and b = 2"], workload_count=2, **_positive_seed_kwargs())

    assert all(len(key[1]) == 1 for key in res.topk_set)
    assert res.stats["rejected_growth_parse_fallback"] == 1


def test_no_range_prefix_composites_and_no_width_above_two(tmp_path, monkeypatch):
    gen = _gen(tmp_path, monkeypatch)
    workload = ["q1\tselect * from t where c > 3 and a = 1"]

    res = gen.generate(workload, workload_count=2, **_positive_seed_kwargs(("t", ("c",))))

    assert all(len(key[1]) <= 2 for key in res.topk_set)
    assert not any(len(key[1]) == 2 and key[1][0] == "c" for key in res.topk_set)
    assert not any(len(key[1]) == 2 and res.meta_map[key]["family"] == "RANGE_RANGE" for key in res.meta_map)


def test_leading_wildcard_like_is_not_range_but_prefix_like_is(tmp_path, monkeypatch):
    gen = _gen(tmp_path, monkeypatch)

    leading = gen.extractor.extract_line("q1\tselect * from t where a like '%abc%'", 0)
    prefix = gen.extractor.extract_line("q1\tselect * from t where a like 'abc%'", 1)

    assert "a" not in leading.filter_rng.get("t", [])
    assert "a" in prefix.filter_rng.get("t", [])


def test_or_query_blocks_cross_branch_composite(tmp_path, monkeypatch):
    gen = _gen(tmp_path, monkeypatch)
    evidence = _ast_evidence(has_or=True)
    res = _generate_with_evidence(
        gen,
        evidence,
        workload_count=2,
        **_positive_seed_kwargs(),
    )

    assert all(len(key[1]) == 1 for key in res.topk_set)
    assert res.stats["rejected_growth_has_or"] >= 1


def test_uncertain_alias_blocks_composite_growth(tmp_path, monkeypatch):
    gen = _gen(tmp_path, monkeypatch)
    evidence = QueryEvidence(
        query_id=0,
        template_id="q1",
        sql="select * from t x join t y on x.a = y.a where x.b = 1",
        tables={"t"},
        filter_eq={"t": ["a", "b"]},
        alias_ambiguous_tables={"t"},
        parse_status="ast_ok",
    )
    singles = gen._emit_single_probes(evidence)
    rejected = Counter()
    grown = gen._grow_width2(evidence, singles, gen._make_seed_states(**_positive_seed_kwargs()), rejected, {})

    assert grown == {}


def test_every_width2_candidate_has_seed_provenance(tmp_path, monkeypatch):
    gen = _gen(tmp_path, monkeypatch)
    evidence = _ast_evidence()
    res = _generate_with_evidence(
        gen,
        evidence,
        workload_count=2,
        **_positive_seed_kwargs(),
    )

    width2 = [key for key in res.topk_set if len(key[1]) == 2]
    assert width2
    for key in width2:
        meta = res.meta_map[key]
        assert meta["seed_key"]
        assert meta["seed_benefit"] > 0
        assert meta["seed_evaluated_count"] > 0
        assert meta["seed_positive_count"] > 0
