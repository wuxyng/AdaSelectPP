import ast
import csv
import inspect

import pytest

import tools.pr20e_broader_prefix_swap_replay as pr20e


PREFIX = ("movie_info", ("mi_movie_id",))
COMPOSITE = ("movie_info", ("mi_movie_id", "mi_info_type_id"))
OTHER = ("cast_info", ("ci_movie_id",))


def _write_pr20c_inputs(tmp_path):
    rounds_path = tmp_path / "rounds.csv"
    candidates_path = tmp_path / "candidates.csv"
    with rounds_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["round_id", "best_swap_index", "best_swap_relative_improvement"])
        writer.writeheader()
        for rid, rel in [
            (1, 0.09),
            (2, 0.08),
            (3, 0.07),
            (4, 0.05),
            (5, 0.03),
            (6, 0.02),
            (7, 0.001),
            (8, -0.002),
        ]:
            writer.writerow({
                "round_id": rid,
                "best_swap_index": "movie_info(mi_movie_id,mi_info_type_id)",
                "best_swap_relative_improvement": rel,
            })
    with candidates_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["round_id", "width2_index", "swap_relative_improvement", "oracle_pass_swap"],
        )
        writer.writeheader()
        for rid, rel in [
            (1, 0.09),
            (2, 0.08),
            (3, 0.07),
            (4, 0.05),
            (5, 0.03),
            (6, 0.02),
            (7, 0.001),
            (8, -0.002),
        ]:
            writer.writerow({
                "round_id": rid,
                "width2_index": "movie_info(mi_movie_id,mi_info_type_id)",
                "swap_relative_improvement": rel,
                "oracle_pass_swap": 1 if rel >= 0.005 else 0,
            })
    return rounds_path, candidates_path


def test_round_selection_produces_top_mid_low_control_categories(tmp_path):
    rounds_path, candidates_path = _write_pr20c_inputs(tmp_path)

    selected = pr20e.select_broader_rounds(
        rounds_csv=rounds_path,
        candidates_csv=candidates_path,
        composite_index=COMPOSITE,
        selection_mode="all",
    )

    categories = {item.sample_category for item in selected}
    assert {"top_win", "mid_win", "low_win", "control"} <= categories
    assert [item.round_id for item in selected] == list(range(1, 9))


def test_excluded_unstable_is_reported_when_cv_exceeds_cap():
    reason = pr20e.unstable_reason_for(base_cv=0.25, swap_cv=0.1, max_cv=0.20)
    assert reason == "baseline_cv_high"
    assert pr20e.outcome_for_rel(0.5, unstable=True) == "excluded_unstable"


def test_excluded_unstable_rounds_are_not_in_primary_aggregates():
    round_rows = [
        {
            "sample_category": "top_win",
            "unstable_excluded": 0,
            "real_exec_rel_improvement": 0.10,
            "outcome": "improved",
            "pr20c_whatif_rel_improvement": 0.05,
        },
        {
            "sample_category": "excluded_unstable",
            "unstable_excluded": 1,
            "real_exec_rel_improvement": 0.99,
            "outcome": "excluded_unstable",
            "pr20c_whatif_rel_improvement": 0.04,
        },
    ]
    excluded_rows = [{"round_id": 2, "unstable_reason": "swap_cv_high", "baseline_cv": 0.1, "swap_cv": 0.3}]

    summary = pr20e.summarize_results(round_rows, excluded_rows)
    top_row = next(row for row in summary if row["sample_category"] == "top_win")
    excluded_row = next(row for row in summary if row["row_type"] == "excluded_unstable")

    assert top_row["round_count"] == 1
    assert top_row["mean_real_exec_rel_improvement"] == 0.10
    assert excluded_row["excluded_round_count"] == 1
    assert excluded_row["excluded_round_ids"] == "2"


def test_baseline_swap_configs_differ_only_by_prefix_to_composite():
    swapped, feasible, reason = pr20e.build_prefix_swap_config(
        {PREFIX, OTHER},
        PREFIX,
        COMPOSITE,
        max_num=2,
    )

    assert feasible
    assert reason == ""
    assert swapped == {COMPOSITE, OTHER}


def test_tool_refuses_physical_execution_without_flag():
    with pytest.raises(PermissionError):
        pr20e.ensure_experimental_allowed(
            experimental_physical_indexes=False,
            database="job",
        )


def test_descriptive_ordering_label_is_emitted_and_documented():
    summary = pr20e.summarize_results(
        [
            {
                "sample_category": "top_win",
                "unstable_excluded": 0,
                "real_exec_rel_improvement": 0.2,
                "outcome": "improved",
                "pr20c_whatif_rel_improvement": 0.1,
            },
            {
                "sample_category": "control",
                "unstable_excluded": 0,
                "real_exec_rel_improvement": 0.05,
                "outcome": "improved",
                "pr20c_whatif_rel_improvement": 0.0,
            },
        ],
        [],
    )
    ordering = next(row for row in summary if row["row_type"] == "descriptive_ordering")

    assert ordering["ordering_diagnostic_label"] == pr20e.DESCRIPTIVE_ONLY_LABEL
    assert "DESCRIPTIVE ONLY" in pr20e.DESCRIPTIVE_ONLY_LABEL


def test_pr20e_tool_does_not_import_online_policy_or_candidate_generation():
    source = inspect.getsource(pr20e)
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

