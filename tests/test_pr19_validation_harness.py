import ast
import inspect
import json
from pathlib import Path

from tools.analyze_pr19 import compute_round_deltas, jaccard
import tools.offline_validate_candidate_pool as offline_validator
from tools.export_candidate_pools import export_candidate_pools
from tools.pr19_candidate_pool_common import (
    LITESELECT_TWOCELF_IMPORTED,
    SELECTOR_NAME,
    SELECTOR_SEMANTICS,
    format_candidate_key,
    normalize_candidate_key,
    normalized_candidate_strings,
    parse_candidate_string,
)


class FakeDB:
    def __init__(self):
        self._cols = {"t": ["a", "b", "c"]}

    def get_tables(self):
        return list(self._cols)

    def get_columns(self, table):
        return list(self._cols[str(table)])

    def exec_fetchall(self, _sql):
        return []


def test_pr19_candidate_normalization_is_stable():
    assert normalize_candidate_key(("T", ("A", "B"))) == ("t", ("a", "b"))
    assert normalize_candidate_key(["t", ["a", "b", "a"]]) == ("t", ("a", "b"))
    assert parse_candidate_string("T(A, B)") == ("t", ("a", "b"))
    assert format_candidate_key(("T", ("A", "B"))) == "t(a,b)"
    assert normalized_candidate_strings([("t", ("b",)), ("T", ("A", "B")), "t(b)"]) == [
        "t(a,b)",
        "t(b)",
    ]


def test_pr19_analyzer_jaccard_and_deltas():
    key = ("tpchs", "random", 7)
    probe = {
        key: {
            "selector_name": "offline_pool_celf",
            "selector_semantics": "pool_restricted_deterministic",
            "liteselect_twocelf_imported": "false",
            "pool_size": "10",
            "width2_count": "1",
            "selected_width2": "0",
            "relative_improvement": "0.10",
            "selected_indexes": "lineitem(l_partkey);orders(o_custkey)",
            "selector_time_ms": "5.0",
        }
    }
    fair = {
        key: {
            "selector_name": "offline_pool_celf",
            "selector_semantics": "pool_restricted_deterministic",
            "liteselect_twocelf_imported": "false",
            "pool_size": "12",
            "width2_count": "3",
            "selected_width2": "1",
            "relative_improvement": "0.15",
            "selected_indexes": "lineitem(l_partkey);lineitem(l_partkey,l_shipdate)",
            "selector_time_ms": "8.0",
        }
    }

    rows = compute_round_deltas(
        probe,
        fair,
        {key: 2.0},
        {key: 6.5},
    )

    assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert len(rows) == 1
    row = rows[0]
    assert row["pool_size_delta"] == 2
    assert row["width2_count_delta"] == 2
    assert row["selected_width2_delta"] == 1
    assert abs(row["improvement_delta"] - 0.05) < 1e-12
    assert row["selected_overlap_jaccard"] == 1 / 3
    assert row["fair_win"] == 1
    assert row["generation_time_delta"] == 4.5
    assert row["selector_time_delta"] == 3.0


def test_pr19_analyzer_fails_on_selector_metadata_mismatch():
    key = ("tpchs", "random", 1)
    probe = {
        key: {
            "selector_name": "offline_pool_celf",
            "selector_semantics": "pool_restricted_deterministic",
            "liteselect_twocelf_imported": "false",
            "pool_size": "1",
            "width2_count": "0",
            "selected_width2": "0",
            "relative_improvement": "0.1",
            "selected_indexes": "t(a)",
            "selector_time_ms": "1.0",
        }
    }
    fair = {
        key: {
            "selector_name": "different_selector",
            "selector_semantics": "pool_restricted_deterministic",
            "liteselect_twocelf_imported": "false",
            "pool_size": "1",
            "width2_count": "0",
            "selected_width2": "0",
            "relative_improvement": "0.1",
            "selected_indexes": "t(a)",
            "selector_time_ms": "1.0",
        }
    }

    try:
        compute_round_deltas(probe, fair)
    except ValueError as exc:
        assert "selector metadata mismatch" in str(exc)
        assert "selector_name" in str(exc)
    else:
        raise AssertionError("expected selector metadata mismatch")


def test_pr19_offline_validator_is_pool_restricted_and_not_twocelf():
    source = inspect.getsource(offline_validator)
    tree = ast.parse(source)
    banned_modules = {
        "adasel.ada_select",
        "adaselect_pp.candidate_gen_v2",
        "adaselect_pp.candidate_gen_v2.generator",
        "litesel.mc.lite_select_mc_twocelf_rpa",
    }
    imported_modules = set()
    referenced_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
        elif isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)

    assert not (imported_modules & banned_modules)
    assert "MCIGCandidateGenerator" not in referenced_names
    assert "LiteSelectMC_TwoCELF" not in referenced_names
    assert "candidate_generator" not in referenced_names


def test_pr19_offline_validator_selects_only_exported_candidates(monkeypatch):
    pool = {"t(a)", "t(b)"}
    pool_keys = {parse_candidate_string(text) for text in pool}

    def fake_evaluate(*, db_con, cost_eval, workload, indexes):
        indexes = set(indexes)
        assert indexes <= pool_keys
        if indexes == {("t", ("a",))}:
            return 70.0
        if indexes == {("t", ("b",))}:
            return 80.0
        if indexes == {("t", ("a",)), ("t", ("b",))}:
            return 60.0
        return 100.0

    monkeypatch.setattr(offline_validator, "_evaluate_config", fake_evaluate)
    row = offline_validator.validate_row(
        row={
            "bench": "fake",
            "workload_type": "random",
            "round_id": 0,
            "mode": "probe_grow",
            "candidates": sorted(pool),
        },
        workload=["q0\tselect 1"],
        db_con=object(),
        cost_eval=object(),
        max_num=2,
    )

    selected = {text for text in row["selected_indexes"].split(";") if text}
    assert selected <= pool
    assert "t(c)" not in selected


def test_pr19_offline_pool_celf_is_deterministic(monkeypatch):
    costs = {
        frozenset(): 100.0,
        frozenset({("t", ("a",))}): 70.0,
        frozenset({("t", ("b",))}): 80.0,
        frozenset({("t", ("a",)), ("t", ("b",))}): 60.0,
    }

    def fake_evaluate(*, db_con, cost_eval, workload, indexes):
        return costs[frozenset(indexes)]

    monkeypatch.setattr(offline_validator, "_evaluate_config", fake_evaluate)
    exported_row = {
        "bench": "fake",
        "workload_type": "random",
        "round_id": 0,
        "mode": "probe_grow_fair",
        "candidates": ["t(b)", "t(a)"],
    }

    first = offline_validator.validate_row(
        row=exported_row,
        workload=["q0\tselect 1"],
        db_con=object(),
        cost_eval=object(),
        max_num=2,
    )
    second = offline_validator.validate_row(
        row=exported_row,
        workload=["q0\tselect 1"],
        db_con=object(),
        cost_eval=object(),
        max_num=2,
    )

    assert first["selected_indexes"] == second["selected_indexes"]
    assert first["relative_improvement"] == second["relative_improvement"]


def test_pr19_offline_validator_metadata_names_oracle():
    assert offline_validator.CSV_COLUMNS[4:7] == [
        "selector_name",
        "selector_semantics",
        "liteselect_twocelf_imported",
    ]
    row = offline_validator.validate_row(
        row={
            "bench": "fake",
            "workload_type": "random",
            "round_id": 0,
            "mode": "probe_grow",
            "candidates": [],
        },
        workload=["q0\tselect 1"],
        db_con=type("FakeDB", (), {"drop_all_indexes": lambda self: None, "create_index": lambda self, *_: None})(),
        cost_eval=type("FakeCost", (), {"calculate_now_cost": lambda self, workload: 10.0})(),
        max_num=10,
    )
    assert row["selector_name"] == SELECTOR_NAME == "offline_pool_celf"
    assert row["selector_semantics"] == SELECTOR_SEMANTICS == "pool_restricted_deterministic"
    assert row["liteselect_twocelf_imported"] == LITESELECT_TWOCELF_IMPORTED == "false"


def test_pr19_export_smoke_tiny_workload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    txt_dir = tmp_path / "txt"
    txt_dir.mkdir()
    (txt_dir / "fake_indexable_columns.txt").write_text("t a\nt b\nt c\n", encoding="utf-8")
    workloads = [
        ["q0\tselect * from t where a = 1 and b > 2"],
        ["q1\tselect * from t where a = 2 and b > 3"],
    ]

    paths = export_candidate_pools(
        bench="fake",
        workload_type="random",
        workloads=workloads,
        db_con=FakeDB(),
        output_root=tmp_path / "runs_pr19_candidate_pool",
        cfg={"max_num": 2, "candidate_topk_factor": 2, "candidate_topk_min_extra": 0},
    )

    assert set(paths) == {"probe_grow", "probe_grow_fair"}
    for path in paths.values():
        assert path.exists()
        rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        assert rows[0]["bench"] == "fake"
        assert rows[0]["workload_type"] == "random"
        assert rows[0]["num_candidates"] == len(rows[0]["candidates"])
        assert isinstance(rows[0]["candidate_source_stats"], dict)
