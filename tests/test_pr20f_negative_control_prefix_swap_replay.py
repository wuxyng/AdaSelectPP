import ast
import csv
import inspect
from pathlib import Path

import pytest

import tools.pr20f_negative_control_prefix_swap_replay as pr20f


PREFIX = ("movie_info", ("mi_movie_id",))
COMPOSITE = ("movie_info", ("mi_movie_id", "mi_info_type_id"))
OTHER = ("cast_info", ("ci_movie_id",))
ALT_WIDTH2 = "cast_info(ci_movie_id,ci_role_id)"
TARGET = "movie_info(mi_movie_id,mi_info_type_id)"


def _write_metrics(tmp_path: Path) -> Path:
    path = tmp_path / "metrics.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["round", "old", "new"])
        writer.writeheader()
        old_config = "[(\"movie_info\", (\"mi_movie_id\",)), (\"cast_info\", (\"ci_movie_id\",))]"
        new_config = "[(\"movie_info\", (\"mi_movie_id\", \"mi_info_type_id\")), (\"cast_info\", (\"ci_movie_id\",))]"
        for rid in [1, 2, 3, 4, 22]:
            writer.writerow({"round": rid, "old": old_config, "new": new_config})
    return path


def _write_pr20c_inputs(tmp_path: Path):
    rounds_path = tmp_path / "rounds.csv"
    candidates_path = tmp_path / "candidates.csv"
    rows = [
        # Positive-arm census: target is best and passes. PR20f excludes it by default.
        (1, TARGET, 0.09, 0.09, 1),
        # Positive target signal, but a different width-2 candidate is best.
        (2, ALT_WIDTH2, 0.07, 0.04, 1),
        # Small positive target signal below the default margin.
        (3, TARGET, 0.002, 0.002, 0),
        # Non-positive target signal.
        (4, ALT_WIDTH2, 0.01, -0.01, 0),
        # PR20e warning reference.
        (22, TARGET, 0.0262, 0.0262, 1),
    ]
    with rounds_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["round_id", "best_swap_index", "best_swap_relative_improvement"])
        writer.writeheader()
        for rid, best_index, best_rel, _target_rel, _pass in rows:
            writer.writerow({
                "round_id": rid,
                "best_swap_index": best_index,
                "best_swap_relative_improvement": best_rel,
            })
    with candidates_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["round_id", "width2_index", "swap_relative_improvement", "oracle_pass_swap"],
        )
        writer.writeheader()
        for rid, _best_index, _best_rel, target_rel, oracle_pass in rows:
            writer.writerow({
                "round_id": rid,
                "width2_index": TARGET,
                "swap_relative_improvement": target_rel,
                "oracle_pass_swap": oracle_pass,
            })
    return rounds_path, candidates_path


def test_selection_covers_negative_control_categories_and_excludes_positive_arm(tmp_path):
    metrics = _write_metrics(tmp_path)
    rounds, candidates = _write_pr20c_inputs(tmp_path)

    selected = pr20f.select_negative_control_rounds(
        rounds_csv=rounds,
        candidates_csv=candidates,
        metrics_csv=metrics,
        prefix_index=PREFIX,
        composite_index=COMPOSITE,
        max_num=10,
    )

    by_round = {item.round_id: item.sample_category for item in selected}
    assert 1 not in by_round
    assert by_round[2] == "non_target_best_positive"
    assert by_round[3] == "predicted_flat_or_low"
    assert by_round[4] == "predicted_negative"
    assert by_round[22] == "near_margin"


def test_read_executed_config_uses_old_not_new(tmp_path):
    metrics = _write_metrics(tmp_path)

    configs = pr20f.read_executed_configs(metrics)

    assert PREFIX in configs[1]
    assert COMPOSITE not in configs[1]


def test_baseline_swap_configs_differ_only_by_prefix_to_composite():
    swapped, feasible, reason = pr20f.build_prefix_swap_config(
        {PREFIX, OTHER},
        PREFIX,
        COMPOSITE,
        max_num=2,
    )

    assert feasible
    assert reason == ""
    assert swapped == {COMPOSITE, OTHER}


def test_physical_execution_refuses_without_experimental_flag():
    with pytest.raises(PermissionError):
        pr20f.ensure_experimental_allowed(
            experimental_physical_indexes=False,
            database="job",
        )


def test_unstable_rounds_are_emitted_and_excluded_from_primary_aggregates():
    stable = {
        "round_id": 1,
        "sample_category": "predicted_flat_or_low",
        "unstable_excluded": 0,
        "real_exec_rel_improvement": 0.02,
        "real_outcome": "improved",
        "gate_threshold": 0.03,
    }
    unstable = {
        "round_id": 2,
        "sample_category": "excluded_unstable",
        "unstable_excluded": 1,
        "real_exec_rel_improvement": 0.50,
        "real_outcome": "excluded_unstable",
        "gate_threshold": 0.03,
    }
    excluded = [{"round_id": 2, "unstable_reason": "swap_cv_high"}]

    summary = pr20f.summarize_category_metrics([stable, unstable], excluded)
    flat_row = next(row for row in summary if row["sample_category"] == "predicted_flat_or_low")
    excluded_row = next(row for row in summary if row["row_type"] == "excluded_unstable")

    assert flat_row["round_count"] == 1
    assert flat_row["improved_count"] == 1
    assert excluded_row["excluded_round_count"] == 1
    assert excluded_row["excluded_round_ids"] == "2"


def test_gate_threshold_simulation_produces_all_gate_outcomes():
    rows = []
    for rid, rel, real_outcome in [
        (1, 0.04, "improved"),
        (2, 0.04, "flat"),
        (3, 0.02, "flat"),
        (4, 0.02, "improved"),
    ]:
        rows.extend(pr20f.expand_gate_rows(
            {
                "round_id": rid,
                "sample_category": "predicted_flat_or_low",
                "unstable_excluded": 0,
                "target_swap_whatif_rel_improvement": rel,
                "real_outcome": real_outcome,
            },
            gate_thresholds=[0.03],
        ))

    assert {row["gate_outcome"] for row in rows} == {
        "true_accept",
        "false_accept",
        "true_reject",
        "false_reject",
    }


def test_gate_metrics_are_computed_per_threshold():
    rows = []
    for rel, real_outcome in [(0.04, "improved"), (0.04, "flat"), (0.02, "flat"), (0.02, "improved")]:
        rows.extend(pr20f.expand_gate_rows(
            {
                "round_id": len(rows),
                "unstable_excluded": 0,
                "target_swap_whatif_rel_improvement": rel,
                "real_outcome": real_outcome,
            },
            gate_thresholds=[0.03, 0.05],
        ))

    metrics = pr20f.summarize_gate_metrics(rows)
    by_threshold = {row["threshold"]: row for row in metrics}

    assert set(by_threshold) == {0.03, 0.05}
    assert by_threshold[0.03]["tested_count"] == 4
    assert by_threshold[0.03]["accept_count"] == 2
    assert by_threshold[0.03]["true_accept_count"] == 1
    assert by_threshold[0.03]["false_accept_count"] == 1
    assert by_threshold[0.03]["true_reject_count"] == 1
    assert by_threshold[0.03]["false_reject_count"] == 1


def test_docs_and_output_labels_avoid_model_fit_claim_language():
    doc = Path("docs/pr20f_negative_control_prefix_swap_replay.md").read_text(encoding="utf-8").lower()
    output_terms = " ".join(pr20f.ROUND_COLUMNS + pr20f.SUMMARY_COLUMNS + pr20f.GATE_METRICS_COLUMNS).lower()
    banned = ["calibration", "bias", "underestimation", "overestimation"]

    for word in banned:
        assert word not in doc
        assert word not in output_terms


def test_pr20f_tool_does_not_import_online_policy_or_candidate_generation():
    source = inspect.getsource(pr20f)
    tree = ast.parse(source)
    banned_modules = {
        "adasel.ada_select",
        "adaselect_pp.candidate_gen_v2",
        "adaselect_pp.candidate_gen_v2.generator",
        "adaselect_pp.selector",
        "adaselect_pp.materialization",
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
    assert "_choose_config" not in source
    assert "MCIGCandidateGenerator" not in referenced_names
    assert "candidate_generator" not in referenced_names
