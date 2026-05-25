import json
from pathlib import Path

import pytest

from adasel.ada_select import AdaSelect
from adaselect_pp.candidate_gen_v2.generator import MCIGCandidateGenerator
from adaselect_pp.candidate_gen_v2.vocabulary import ColumnVocabulary
from util.benefit_normalizer import BenefitNormalizer


class FakeDB:
    def __init__(self):
        self._cols = {
            "t": ["a", "b", "c"],
            "u": ["a", "d"],
        }

    def get_tables(self):
        return list(self._cols)

    def get_columns(self, table):
        return list(self._cols[str(table)])

    def exec_fetchall(self, _sql):
        return []


def test_adaselect_config_default_max_width_is_two():
    cfg = json.loads(Path("adasel/config/adaselect.json").read_text(encoding="utf-8"))
    assert cfg["max_width"] == 2


def test_adaselect_rejects_width_above_phase05_scope():
    with pytest.raises(ValueError, match="max_width <= 2"):
        AdaSelect("tpch", None, None, None, cfg_source={"max_width": 3})


def test_adaselect_rejects_disabled_wdcg_noop_switch():
    with pytest.raises(ValueError, match="wdcg_enabled=false"):
        AdaSelect("tpch", None, None, None, cfg_source={"wdcg_enabled": False})


def test_generator_rejects_width_above_phase05_scope():
    with pytest.raises(ValueError, match="max_width <= 2"):
        MCIGCandidateGenerator("fake", FakeDB(), max_width=3)


def test_whitelist_required_missing_and_empty_fail_fast(tmp_path):
    missing = tmp_path / "missing_indexable_columns.txt"
    with pytest.raises(FileNotFoundError):
        ColumnVocabulary.load("fake", db_con=FakeDB(), explicit_path=str(missing), required=True)

    empty = tmp_path / "empty_indexable_columns.txt"
    empty.write_text("# no columns\n", encoding="utf-8")
    with pytest.raises(ValueError, match="parsed empty"):
        ColumnVocabulary.load("fake", db_con=FakeDB(), explicit_path=str(empty), required=True)


def test_whitelist_load_records_path_and_counts(tmp_path):
    whitelist = tmp_path / "fake_indexable_columns.txt"
    whitelist.write_text("t a\nt b\nu d\n", encoding="utf-8")

    vocab = ColumnVocabulary.load("fake", db_con=FakeDB(), explicit_path=str(whitelist), required=True)

    assert vocab.enabled
    assert vocab.path == str(whitelist)
    assert len(vocab.mapping) == 2
    assert sum(len(cols) for cols in vocab.mapping.values()) == 3
    assert vocab.is_allowed("t", "a")
    assert not vocab.is_allowed("t", "d")


def test_creation_cost_required_missing_empty_and_loaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "txt").mkdir()

    norm = BenefitNormalizer()
    with pytest.raises(FileNotFoundError):
        norm.load_creation_costs("fake", required=True)
    assert norm.creation_cost_status == "missing"

    (tmp_path / "txt" / "fake_op_3_create_time.txt").write_text("bad line\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No creation-cost records parsed"):
        norm.load_creation_costs("fake", required=True)
    assert norm.creation_cost_status == "empty"

    (tmp_path / "txt" / "fake_op_3_create_time.txt").write_text(
        "('a',) 10.0\n('a', 'b') 20.0\n",
        encoding="utf-8",
    )
    norm.load_creation_costs("fake", required=True)
    assert norm.creation_cost_status == "loaded"
    assert norm.creation_cost_entries == 2
    assert ("a",) in norm.index_costs


def test_generator_reports_sqlglot_and_whitelist_diagnostics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "txt").mkdir()
    (tmp_path / "txt" / "fake_indexable_columns.txt").write_text("t a\nt b\n", encoding="utf-8")

    gen = MCIGCandidateGenerator("fake", FakeDB(), max_width=2)
    res = gen.generate(["q1\tselect * from t where a = 1 and b > 2"], topk=10)

    assert res.stats["vocab_enabled"] == 1
    assert res.stats["vocab_path"].endswith("fake_indexable_columns.txt")
    assert res.stats["vocab_tables"] == 1
    assert res.stats["vocab_columns"] == 2
    assert res.stats["sqlglot_available"] in (0, 1)
