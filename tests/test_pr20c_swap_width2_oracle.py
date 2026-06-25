import ast
import csv
import inspect

import tools.pr20c_swap_width2_oracle as oracle


A = ("t", ("a",))
B = ("t", ("b",))
X = ("t", ("x",))
PAIR_AB = ("t", ("a", "b"))
PAIR_XY = ("t", ("x", "y"))


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_swap_config_removes_selected_prefix_and_adds_width2_atomically():
    config, prefix, feasible, reason = oracle.build_swap_config(
        {A, B, X},
        PAIR_AB,
        max_num=3,
    )

    assert feasible
    assert reason == ""
    assert prefix == A
    assert config == {B, X, PAIR_AB}
    assert A not in config
    assert len(config) == 3


def test_add_config_respects_max_num_capacity():
    config, feasible, reason = oracle.build_add_config(
        {A, X},
        PAIR_AB,
        max_num=2,
    )

    assert config is None
    assert not feasible
    assert reason == "add_infeasible_due_to_capacity"

    existing_config, existing_feasible, existing_reason = oracle.build_add_config(
        {A, PAIR_AB},
        PAIR_AB,
        max_num=2,
    )
    assert existing_feasible
    assert existing_reason == ""
    assert existing_config == {A, PAIR_AB}


def test_oracle_outputs_baseline_add_swap_rows_for_tiny_synthetic_pool(tmp_path):
    metrics_path = tmp_path / "metrics.csv"
    metrics_path.write_text(
        "round,new\n"
        "0,\"[('t', ('a',)), ('t', ('x',))]\"\n",
        encoding="utf-8",
    )
    trace_path = tmp_path / "trace.csv"
    trace_path.write_text(
        "round,table,cols,in_appearing\n"
        "0,t,\"a,b\",1\n",
        encoding="utf-8",
    )
    costs = {
        frozenset({A, X}): 100.0,
        frozenset({A, X, PAIR_AB}): 95.0,
        frozenset({X, PAIR_AB}): 80.0,
    }

    def fake_evaluate(_workload, indexes):
        return costs[frozenset(indexes)]

    candidates_path, rounds_path, summary_path = oracle.run_oracle(
        benchmark="job",
        workload_type="random",
        workloads=[["q0\tselect 1"]],
        metrics_csv=metrics_path,
        trace_csv=trace_path,
        output_root=tmp_path / "out",
        max_num=3,
        threshold=0.005,
        evaluate_config=fake_evaluate,
    )

    candidate_rows = _read_csv(candidates_path)
    round_rows = _read_csv(rounds_path)
    summary_rows = _read_csv(summary_path)

    assert len(candidate_rows) == 1
    row = candidate_rows[0]
    assert row["width2_index"] == "t(a,b)"
    assert row["baseline_cost"] == "100"
    assert row["add_config"] == "t(a);t(a,b);t(x)"
    assert row["add_cost"] == "95"
    assert row["add_relative_improvement"] == "0.05"
    assert row["swap_prefix_index"] == "t(a)"
    assert row["swap_config"] == "t(a,b);t(x)"
    assert row["swap_cost"] == "80"
    assert row["swap_relative_improvement"] == "0.2"
    assert row["best_mode"] == "swap"
    assert row["oracle_pass_add"] == "1"
    assert row["oracle_pass_swap"] == "1"

    assert round_rows[0]["round_id"] == "0"
    assert round_rows[0]["num_width2_candidates_tested"] == "1"
    assert round_rows[0]["num_add_feasible"] == "1"
    assert round_rows[0]["num_swap_feasible"] == "1"
    assert round_rows[0]["add_oracle_win"] == "1"
    assert round_rows[0]["swap_oracle_win"] == "1"
    assert summary_rows[0]["rounds"] == "1"
    assert summary_rows[0]["tested_width2_candidates"] == "1"
    assert summary_rows[0]["swap_win_rounds"] == "1"


def test_pr20c_tool_does_not_import_online_policy_or_candidate_generation():
    source = inspect.getsource(oracle)
    tree = ast.parse(source)
    banned_modules = {
        "adasel.ada_select",
        "adaselect_pp.candidate_gen_v2",
        "adaselect_pp.candidate_gen_v2.generator",
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
    assert "AdaSelect" not in referenced_names
    assert "MCIGCandidateGenerator" not in referenced_names
    assert "candidate_generator" not in referenced_names
