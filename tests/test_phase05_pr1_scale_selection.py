from pathlib import Path
from types import SimpleNamespace

from adasel.ada_select import AdaSelect
from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from util.benefit_normalizer import BenefitNormalizer


class FakeDB:
    def __init__(self):
        self._cols = {
            "t": ["a", "b", "c", "shared"],
            "u": ["shared", "d"],
        }

    def get_tables(self):
        return list(self._cols)

    def get_columns(self, table):
        return list(self._cols[str(table)])

    def exec_fetchall(self, _sql):
        return []


def _write_whitelist(tmp_path: Path) -> None:
    (tmp_path / "txt").mkdir()
    (tmp_path / "txt" / "fake_indexable_columns.txt").write_text(
        "t a\nt b\nt c\nt shared\nu shared\nu d\n",
        encoding="utf-8",
    )


def test_raw_benefit_does_not_change_generator_static_scores(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_whitelist(tmp_path)
    gen = MCIGCandidateGenerator("fake", FakeDB(), max_width=2, max_num=20)
    workload = ["q1\tselect * from t where a = 1 and b = 2 and c > 3"]

    low = gen.generate(workload, mu_table={("t", ("a",)): 1.0}, topk=20)
    high = gen.generate(workload, mu_table={("t", ("a",)): 1_000_000.0}, topk=20)

    assert low.score_map == high.score_map
    assert {
        key: meta["score"] for key, meta in low.meta_map.items()
    } == {
        key: meta["score"] for key, meta in high.meta_map.items()
    }
    assert high.stats["raw_benefit_in_generator_score"] is False


def test_first_round_filters_nonpositive_normalized_net_utility():
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.columns_benefit = {
        ("t", ("a",)): 0.5,
        ("t", ("b",)): 0.0,
        ("t", ("c",)): -0.2,
    }
    tuner.max_num = 10
    tuner.workload_count = 0
    tuner.transition_mode = "symmetric"
    tuner.beta = 1.1
    tuner.benefit_norm = SimpleNamespace(index_costs={("a",): 0.0, ("b",): 0.0, ("c",): 0.0})
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

    selected = tuner._choose_config(set())

    assert selected == {("t", ("a",))}
    assert tuner._last_candidate_conf == {("t", ("a",))}
    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 2.0
    assert tuner._m_stats["filtered_nonpositive_count"] == 2


def test_first_round_filters_positive_benefit_when_creation_cost_makes_net_nonpositive():
    tuner = AdaSelect.__new__(AdaSelect)
    cheap = ("t", ("a",))
    expensive = ("t", ("b",))
    tuner.columns_benefit = {
        cheap: 10.0,
        expensive: 5.0,
    }
    tuner.max_num = 10
    tuner.workload_count = 0
    tuner.transition_mode = "symmetric"
    tuner.beta = 1.1
    tuner.benefit_norm = SimpleNamespace(index_costs={("a",): 0.0, ("b",): 0.5})
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

    selected = tuner._choose_config(set())

    assert selected == {cheap}
    assert expensive not in tuner._last_candidate_conf
    assert tuner._last_net_benefit_map[expensive] <= 0.0
    assert tuner._last_decision_stats["filtered_nonpositive_count"] == 1.0


def test_creation_cost_lookup_prefers_table_aware_mapping(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "txt").mkdir()
    (tmp_path / "txt" / "fake_op_3_create_time.txt").write_text(
        "('a',) 10.0\n('d',) 20.0\n",
        encoding="utf-8",
    )
    norm = BenefitNormalizer()
    norm.load_creation_costs("fake", required=True, db_con=FakeDB())

    assert ("t", ("a",)) in norm.index_costs_by_key
    assert ("u", ("d",)) in norm.index_costs_by_key
    assert norm.creation_cost_for("t", ("a",), 99.0) == norm.index_costs_by_key[("t", ("a",))]


def test_creation_cost_tuple_collision_detected_and_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "txt").mkdir()
    (tmp_path / "txt" / "fake_op_3_create_time.txt").write_text(
        "('shared',) 10.0\n",
        encoding="utf-8",
    )
    norm = BenefitNormalizer()
    norm.load_creation_costs("fake", required=True, db_con=FakeDB())

    assert ("shared",) in norm.creation_cost_collisions
    assert norm.creation_cost_collisions[("shared",)] == {"t", "u"}
    assert norm.creation_cost_for("t", ("shared",), 99.0) == 99.0
