import csv
from pathlib import Path

import tools.pr21f_collect_prefix_upgrade_cost_evidence as pr21f


def write_csv(path: Path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_missing_required_stat_input_is_not_computable(tmp_path: Path):
    path = tmp_path / "pr20f.csv"
    write_csv(
        path,
        ["prefix_index", "composite_index", "prefix_index_size_bytes", "composite_index_size_bytes", "storage_delta_bytes"],
        [{
            "prefix_index": "movie_info(mi_movie_id)",
            "composite_index": "movie_info(mi_movie_id,mi_info_type_id)",
            "prefix_index_size_bytes": "",
            "composite_index_size_bytes": "",
            "storage_delta_bytes": "",
        }],
    )

    audit = pr21f.audit_artifact(pr21f.ArtifactSpec(
        "pr20f_rounds",
        path,
        tuple(pr21f.PR20F_PAIR_COLUMNS),
        ("prefix_index_size_bytes", "composite_index_size_bytes", "storage_delta_bytes"),
    ))
    evidence = pr21f.storage_evidence(
        "movie_info(mi_movie_id) -> movie_info(mi_movie_id,mi_info_type_id)",
        audit.rows,
        None,
        {"BLOCKER_MISSING_STORAGE_DELTA"},
    )

    assert audit.missing_stat_inputs == [
        "prefix_index_size_bytes",
        "composite_index_size_bytes",
        "storage_delta_bytes",
    ]
    assert evidence.status == pr21f.STATUS_NOT_COMPUTABLE
    assert pr21f.SUB_MISSING_STAT in evidence.substatus


def test_missing_write_trace_is_not_computable_no_write_trace():
    evidence = pr21f.write_maintenance_evidence(
        "p -> c",
        [],
        read_only_scope=False,
        pr21e_write_status_values={"BLOCKER_MISSING_WRITE_MAINTENANCE_DELTA"},
    )

    assert evidence.status == pr21f.STATUS_NOT_COMPUTABLE
    assert evidence.value == ""
    assert pr21f.SUB_NO_WRITE_TRACE in evidence.substatus


def test_read_only_scope_is_scoped_estimate_not_zero_write_cost():
    evidence = pr21f.write_maintenance_evidence(
        "p -> c",
        [],
        read_only_scope=True,
        pr21e_write_status_values={"BLOCKER_MISSING_WRITE_MAINTENANCE_DELTA"},
    )

    assert evidence.status == pr21f.STATUS_ESTIMATED_MODEL
    assert evidence.value == ""
    assert pr21f.SUB_READ_ONLY_SCOPE in evidence.substatus
    assert "not generalizable" in evidence.assumptions


def test_pr21e_blocker_status_is_preserved_next_to_estimated_model():
    evidence = pr21f.storage_evidence(
        "p -> c",
        [],
        {
            "storage_delta_bytes_estimate": "1234",
            "storage_model_name": "synthetic_storage_model",
            "storage_model_version": "test-v1",
            "storage_model_assumptions": "synthetic explicit stats",
            "storage_model_parameters": '{"pages": 1}',
        },
        {"BLOCKER_MISSING_STORAGE_DELTA"},
    )

    assert evidence.status == pr21f.STATUS_ESTIMATED_MODEL
    assert evidence.value == "1234"
    assert pr21f.SUB_PR21E_BLOCKER_PRESERVED in evidence.substatus


def test_estimated_model_row_includes_model_provenance():
    evidence = pr21f.storage_evidence(
        "p -> c",
        [],
        {
            "storage_delta_bytes_estimate": "2048",
            "storage_model_name": "explicit_test_model",
            "storage_model_version": "v-test",
            "storage_model_assumptions": "all stats are synthetic",
            "storage_model_parameters": '{"constant": 42}',
        },
        set(),
    )

    assert evidence.source == pr21f.STATUS_ESTIMATED_MODEL
    assert evidence.model == "explicit_test_model"
    assert evidence.model_version == "v-test"
    assert evidence.assumptions == "all stats are synthetic"
    assert evidence.parameters == '{"constant": 42}'


def test_forbidden_payback_roi_net_fields_are_absent():
    assert pr21f.forbidden_fields_absent(pr21f.PAIR_OUTPUT_COLUMNS)


def test_pair_set_mismatch_is_reported_not_silently_union_or_intersection():
    audits = {
        "pr21e_by_round": pr21f.ArtifactAudit(
            pr21f.ArtifactSpec("pr21e_by_round", Path("pr21e.csv")),
            True,
            1,
            "hash",
            pr21f.PR21E_BY_ROUND_COLUMNS,
            [],
            [],
            [],
            [{
                "source_artifact": "pr20e_rounds",
                "row_index": "0",
                "storage_evidence_status": "BLOCKER_MISSING_STORAGE_DELTA",
                "write_maintenance_evidence_status": "BLOCKER_MISSING_WRITE_MAINTENANCE_DELTA",
                "transition_cost_evidence_status": "BLOCKER_MISSING_TRANSITION_COST",
            }],
        ),
        "pr20c_candidates": pr21f.ArtifactAudit(pr21f.ArtifactSpec("pr20c_candidates", Path("c.csv")), True, 0, "hash", [], [], [], [], []),
        "pr20d_rounds": pr21f.ArtifactAudit(pr21f.ArtifactSpec("pr20d_rounds", Path("d.csv")), True, 0, "hash", [], [], [], [], []),
        "pr20e_rounds": pr21f.ArtifactAudit(
            pr21f.ArtifactSpec("pr20e_rounds", Path("e.csv")),
            True,
            2,
            "hash",
            pr21f.PR20E_PAIR_COLUMNS,
            [],
            [],
            [],
            [
                {"prefix_index": "p1", "composite_index": "c1"},
                {"prefix_index": "p2", "composite_index": "c2"},
            ],
        ),
        "pr20f_rounds": pr21f.ArtifactAudit(
            pr21f.ArtifactSpec("pr20f_rounds", Path("f.csv")),
            True,
            1,
            "hash",
            pr21f.PR20F_PAIR_COLUMNS,
            [],
            [],
            [],
            [{"prefix_index": "p3", "composite_index": "c3"}],
        ),
        "cost_stats": pr21f.ArtifactAudit(pr21f.ArtifactSpec("cost_stats", Path("stats.csv")), False, 0, "hash", [], [], [], [], []),
        "write_trace": pr21f.ArtifactAudit(pr21f.ArtifactSpec("write_trace", Path("write.csv")), False, 0, "hash", [], [], [], [], []),
        "transition_trace": pr21f.ArtifactAudit(pr21f.ArtifactSpec("transition_trace", Path("transition.csv")), False, 0, "hash", [], [], [], [], []),
    }

    pair_rows, mismatch_counts = pr21f.build_pair_rows(audits)

    assert len(pair_rows) == 1
    assert pair_rows[0]["pair_key"] == "p1 -> c1"
    assert pair_rows[0]["pair_source_status"] == pr21f.STATUS_MEASURED
    assert mismatch_counts["pairs_in_pr20e_not_pr21e"] == 1
    assert mismatch_counts["pairs_in_pr20f_not_pr21e"] == 1

    summary = pr21f.build_summary_rows(audits, pair_rows, mismatch_counts)
    online_row = next(row for row in summary if row["metric"] == "PR21b-online")
    assert online_row["value"] == "blocked"
    assert online_row["status"] == pr21f.STATUS_NOT_COMPUTABLE
