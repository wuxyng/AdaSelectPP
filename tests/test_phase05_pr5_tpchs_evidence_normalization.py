from pathlib import Path
from types import SimpleNamespace

from adasel.ada_select import AdaSelect
from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from adaselect_pp.candidate_gen_v2.types import QueryEvidence


class FakeTPCHSDB:
    def __init__(self):
        self._cols = {
            "supplier": ["s_suppkey", "s_name", "s_address", "s_nationkey"],
            "nation": ["n_nationkey", "n_name"],
            "partsupp": ["ps_partkey", "ps_suppkey", "ps_availqty"],
            "part": ["p_partkey", "p_name"],
            "lineitem": ["l_partkey", "l_suppkey", "l_shipdate", "l_quantity"],
        }

    def get_tables(self):
        return list(self._cols)

    def get_columns(self, table):
        return list(self._cols[str(table)])

    def exec_fetchall(self, _sql):
        return []


class FakeJoinDB:
    def __init__(self):
        self._cols = {"t1": ["a"], "t2": ["b"], "t": ["a", "b"]}

    def get_tables(self):
        return list(self._cols)

    def get_columns(self, table):
        return list(self._cols[str(table)])

    def exec_fetchall(self, _sql):
        return []


def _write_whitelist(tmp_path: Path, lines: str, benchmark: str = "fake") -> None:
    (tmp_path / "txt").mkdir(exist_ok=True)
    (tmp_path / "txt" / f"{benchmark}_indexable_columns.txt").write_text(lines, encoding="utf-8")


def _tpchs_gen(tmp_path, monkeypatch) -> MCIGCandidateGenerator:
    monkeypatch.chdir(tmp_path)
    _write_whitelist(
        tmp_path,
        "\n".join(
            [
                "lineitem l_partkey",
                "lineitem l_suppkey",
                "lineitem l_shipdate",
                "supplier s_nationkey",
                "nation n_name",
                "part p_name",
                "partsupp ps_availqty",
            ]
        )
        + "\n",
    )
    return MCIGCandidateGenerator(
        "fake",
        FakeTPCHSDB(),
        max_width=2,
        max_num=80,
        per_query_cap=80,
        per_table_cap=20,
        round_table_cap=20,
    )


def _q20_like_sql() -> str:
    return """
        select s_name, s_address
        from supplier, nation
        where s_suppkey in (
            select ps_suppkey
            from partsupp
            where ps_partkey in (
                select p_partkey
                from part
                where p_name like 'burnished%'
            )
            and ps_availqty > (
                select 0.5 * sum(l_quantity)
                from lineitem
                where l_partkey = ps_partkey
                  and l_suppkey = ps_suppkey
                  and l_shipdate >= date '1993-01-01'
                  and l_shipdate < date '1993-01-01' + interval '1' year
            )
        )
        and s_nationkey = n_nationkey
        and n_name = 'IRAN'
        order by s_name
    """


def test_tpchs_q20_partial_whitelist_join_eq1_and_ranges_are_metadata_visible(tmp_path, monkeypatch):
    gen = _tpchs_gen(tmp_path, monkeypatch)
    sql = _q20_like_sql()

    evidence = gen.extractor.extract_line(f"q20\t{sql}", 0)
    assert "l_partkey" in evidence.join_eq.get("lineitem", [])
    assert "l_suppkey" in evidence.join_eq.get("lineitem", [])
    assert "s_nationkey" in evidence.join_eq.get("supplier", [])

    res = gen.generate([f"q20\t{sql}"], workload_count=0, topk=80)

    expected_families = {
        ("lineitem", ("l_partkey",)): "JOIN_EQ1",
        ("lineitem", ("l_suppkey",)): "JOIN_EQ1",
        ("supplier", ("s_nationkey",)): "JOIN_EQ1",
        ("lineitem", ("l_shipdate",)): "RANGE1",
        ("part", ("p_name",)): "RANGE1",
        ("partsupp", ("ps_availqty",)): "RANGE1",
        ("nation", ("n_name",)): "EQ1",
    }
    for key, family in expected_families.items():
        assert key in res.topk_set
        assert res.meta_map[key]["family"] == family
        assert gen.last_meta[key]["family"] == family


def test_one_sided_whitelist_join_emits_allowed_side_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_whitelist(tmp_path, "t1 a\n")
    gen = MCIGCandidateGenerator(
        "fake",
        FakeJoinDB(),
        max_width=2,
        max_num=20,
        per_query_cap=20,
        per_table_cap=20,
        round_table_cap=20,
    )

    res = gen.generate(["q1\tselect * from t1 join t2 on t1.a = t2.b"], workload_count=0, topk=20)

    assert ("t1", ("a",)) in res.meta_map
    assert res.meta_map[("t1", ("a",))]["family"] == "JOIN_EQ1"
    assert ("t2", ("b",)) not in res.meta_map


def test_regex_fallback_still_does_not_grow_width2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_whitelist(tmp_path, "t a\nt b\n")
    gen = MCIGCandidateGenerator(
        "fake",
        FakeJoinDB(),
        max_width=2,
        max_num=20,
        per_query_cap=20,
        per_table_cap=20,
        round_table_cap=20,
    )
    evidence = QueryEvidence(
        query_id=0,
        template_id="q1",
        sql="select * from t where a = 1 and b = 2",
        tables={"t"},
        filter_eq={"t": ["a", "b"]},
        parse_status="fallback_regex",
    )
    gen.extractor.extract_line = lambda _line, _qid: evidence

    res = gen.generate(
        ["q1\tselect * from t where a = 1 and b = 2"],
        workload_count=2,
        seed_benefit={("t", ("a",)): 1.0},
        seed_seen_count={("t", ("a",)): 1},
        seed_positive_count={("t", ("a",)): 1},
        seed_last_obs_src={("t", ("a",)): "OK"},
    )

    assert all(len(key[1]) == 1 for key in res.topk_set)
    assert res.stats["rejected_growth_parse_fallback"] == 1


def _decision_tuner(benefits, costs):
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.columns_benefit = benefits
    tuner.max_num = 10
    tuner.workload_count = 0
    tuner.transition_mode = "symmetric"
    tuner.beta = 1.1
    tuner.benefit_norm = SimpleNamespace(index_costs=costs)
    tuner._m_stats = {
        "what_if_calls": 0,
        "reconf_add": 0,
        "reconf_drop": 0,
        "trans_create": 0.0,
        "trans_drop": 0.0,
    }
    tuner._last_net_benefit_map = {}
    tuner._last_candidate_conf = set()
    tuner._last_final_conf = set()
    tuner._last_decision_stats = {}
    return tuner


def test_log_positive_norm_keeps_medium_positive_candidate_through_first_round_gate():
    huge = ("lineitem", ("l_shipdate",))
    medium = ("lineitem", ("l_quantity",))
    tuner = _decision_tuner(
        {
            huge: 650_000_000.0,
            medium: 250_000.0,
        },
        {
            ("l_shipdate",): 0.11,
            ("l_quantity",): 0.13,
        },
    )

    selected = tuner._choose_config(set())

    assert huge in selected
    assert medium in selected
    assert tuner._last_net_benefit_map[medium] > 0.0
    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 0.0


def test_zero_and_negative_benefits_still_filter_on_first_round():
    good = ("t", ("a",))
    zero = ("t", ("b",))
    negative = ("t", ("c",))
    tuner = _decision_tuner(
        {
            good: 100.0,
            zero: 0.0,
            negative: -10.0,
        },
        {
            ("a",): 0.0,
            ("b",): 0.0,
            ("c",): 0.0,
        },
    )

    selected = tuner._choose_config(set())

    assert selected == {good}
    assert zero not in tuner._last_candidate_conf
    assert negative not in tuner._last_candidate_conf
    assert tuner._last_net_benefit_map[zero] == 0.0
    assert tuner._last_net_benefit_map[negative] == 0.0
    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 2.0


def test_generator_static_score_still_ignores_raw_benefit(tmp_path, monkeypatch):
    gen = _tpchs_gen(tmp_path, monkeypatch)
    workload = ["q1\tselect * from lineitem where l_shipdate >= date '1993-01-01'"]

    low = gen.generate(workload, seed_benefit={("lineitem", ("l_shipdate",)): 1.0}, topk=20)
    high = gen.generate(workload, seed_benefit={("lineitem", ("l_shipdate",)): 650_000_000.0}, topk=20)

    assert low.score_map == high.score_map
    assert {
        key: meta["score"] for key, meta in low.meta_map.items()
    } == {
        key: meta["score"] for key, meta in high.meta_map.items()
    }
    assert high.stats["raw_benefit_in_generator_score"] is False
