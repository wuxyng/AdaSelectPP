import csv
from pathlib import Path

import tools.pr21e_validate_prefix_upgrade as pr21e


def test_missing_required_column_is_not_computable(tmp_path: Path):
    path = tmp_path / "bad.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["round_id"])
        writer.writeheader()
        writer.writerow({"round_id": "1"})

    audit = pr21e.read_csv_artifact(pr21e.ArtifactSpec(
        "bad",
        path,
        ("round_id", "target_swap_whatif_rel_improvement"),
    ))
    ok, missing = pr21e.require_columns(audit, ["round_id", "target_swap_whatif_rel_improvement"])

    assert not ok
    assert missing == ["target_swap_whatif_rel_improvement"]
    assert audit.missing_columns == ["target_swap_whatif_rel_improvement"]


def test_no_shared_join_key_is_explicitly_not_computable():
    status, notes = pr21e.no_shared_join_key_status()

    assert status == pr21e.NOT_COMPUTABLE_NO_SHARED_JOIN_KEY
    assert "No reliable cross-artifact join key" in notes


def test_zero_whatif_gain_is_online_reject_nonpositive():
    status = pr21e.classify_primary_status("operator_eligible", 0.0)

    assert status == pr21e.STATUS_ONLINE_REJECT


def test_semicolon_config_operator_shape_is_supported():
    baseline = "cast_info(ci_movie_id);movie_info(mi_movie_id)"
    swap = "cast_info(ci_movie_id);movie_info(mi_movie_id,mi_info_type_id)"

    status, notes = pr21e.verify_prefix_upgrade_operator(baseline, swap)

    assert status == "operator_eligible"
    assert notes == "exact_prefix_to_composite_upgrade"


def test_nonpositive_whatif_with_positive_real_evidence_remains_reject_with_conflict_flag():
    primary = pr21e.classify_primary_status("operator_eligible", -0.01)
    flags = pr21e.collect_diagnostic_flags(
        whatif_gain=-0.01,
        real_label="improved",
        sample_category="predicted_negative",
        near_windows=[0.01, 0.02],
        query_level_concentration=None,
        top_query_delta_share=None,
        storage_evidence_missing=True,
        ground_truth_missing=False,
        missing_column=False,
        no_shared_join_key=True,
    )

    assert primary == pr21e.STATUS_ONLINE_REJECT
    assert pr21e.DIAG_CONFLICTING_EVIDENCE in flags
    assert pr21e.DIAG_MISSING_STORAGE in flags


def test_pr20f_gate_metrics_self_check_mismatch_reports_failure():
    round_rows = [
        {"gate_threshold": "0.03", "gate_outcome": "true_accept", "unstable_excluded": "0"},
        {"gate_threshold": "0.03", "gate_outcome": "false_accept", "unstable_excluded": "0"},
    ]
    recomputed = pr21e.recompute_gate_metrics(round_rows)
    historical = [{
        "threshold": "0.03",
        "tested_count": "2",
        "accept_count": "2",
        "reject_count": "0",
        "true_accept_count": "2",
        "false_accept_count": "0",
        "true_reject_count": "0",
        "false_reject_count": "0",
        "accept_precision": "1",
        "reject_success_rate": "",
        "false_accept_rate": "0",
        "false_reject_rate": "",
    }]

    status, diffs = pr21e.compare_gate_metric_rows(recomputed, historical)

    assert status == pr21e.SELF_CHECK_FAILED
    assert any(diff["column"] == "true_accept_count" for diff in diffs)
    assert any(diff["column"] == "false_accept_count" for diff in diffs)


def test_near_margin_diagnostics_are_sweep_not_single_fixed_threshold():
    windows = (0.01, 0.02, 0.03, 0.05)
    matched = pr21e.near_margin_windows(0.015, windows)

    assert matched == [0.02, 0.03, 0.05]

    by_round_rows = [{
        "primary_status": pr21e.STATUS_SHADOW_DEFER,
        "near_margin_windows": "|".join(pr21e.fmt_float(window) for window in matched),
        "diagnostic_flags": pr21e.DIAG_NEAR_MARGIN,
    }]
    summary = pr21e.near_margin_summary(by_round_rows, windows)
    policy = next(row for row in summary if row["metric"] == "policy")

    assert "descriptive-only" in policy["value"]
    assert "no threshold is recommended" in policy["value"]
    assert sum(1 for row in summary if row["metric"].startswith("window_")) == len(windows)
