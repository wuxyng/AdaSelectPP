from argparse import Namespace
from pathlib import Path

import pytest

import tools.pr21g_measure_prefix_upgrade_storage_transition as pr21g


def test_missing_db_url_is_dry_run_not_computable_no_db():
    args = Namespace(schema="public")
    state = pr21g.dry_run_state(args)
    pair = pr21g.Pair(pr21g.DEFAULT_TABLE, pr21g.DEFAULT_PREFIX_COLUMNS, pr21g.DEFAULT_COMPOSITE_COLUMNS)

    row = pr21g.build_pair_row(
        pair,
        "hardcoded_default_asserted",
        pr21g.STATUS_READY_FOR_MEASUREMENT,
        state,
        ddl_is_allowed=False,
        repetitions=3,
    )

    assert state.status == pr21g.STATUS_NO_DB
    assert row["prefix_size_bytes_evidence_source"] == pr21g.EVIDENCE_NOT_COMPUTABLE
    assert row["prefix_size_bytes_status"] == pr21g.STATUS_NO_DB
    assert row["create_composite_ms_status"] == pr21g.STATUS_NO_DB


def test_no_allow_ddl_means_no_ddl_permission():
    assert not pr21g.ddl_allowed("postgresql://example/db", allow_ddl=False, confirm_isolated_db=True)
    assert not pr21g.ddl_allowed("postgresql://example/db", allow_ddl=True, confirm_isolated_db=False)
    assert pr21g.ddl_allowed("postgresql://example/db", allow_ddl=True, confirm_isolated_db=True)


def test_generated_index_names_are_pr21g_prefixed():
    for kind in ("prefix", "composite"):
        name = pr21g.generated_index_name(kind, 7)
        assert name.startswith("pr21g_")
        pr21g.validate_pr21g_index_name(name)


def test_drop_routine_refuses_non_pr21g_owned_indexes():
    with pytest.raises(ValueError):
        pr21g.validate_pr21g_index_name("movie_info_mi_movie_id_idx")

    with pytest.raises(ValueError):
        pr21g.validate_pr21g_index_name("pr21g_bad-name")


def test_empty_table_status_maps_to_not_computable_empty_table():
    assert pr21g.empty_table_status(0) == pr21g.STATUS_EMPTY_TABLE
    assert pr21g.empty_table_status(12) == pr21g.STATUS_READY_FOR_MEASUREMENT


def test_storage_delta_ratio_is_explicitly_named_vs_prefix():
    assert "storage_delta_ratio" not in pr21g.BY_PAIR_COLUMNS
    assert "storage_delta_ratio_vs_prefix" in pr21g.BY_PAIR_COLUMNS
    assert "storage_delta_ratio_vs_prefix_evidence_source" in pr21g.BY_PAIR_COLUMNS


def test_no_benefit_payback_roi_net_fields_in_output_schema():
    assert pr21g.forbidden_fields_absent(pr21g.BY_PAIR_COLUMNS)


def test_report_states_pr21b_online_remains_blocked(tmp_path: Path):
    input_audit = pr21g.InputAudit(
        path=tmp_path / "pr21e.csv",
        exists=False,
        row_count=0,
        content_hash=pr21g.EVIDENCE_NOT_COMPUTABLE,
        columns=(),
        missing_columns=("source_artifact",),
    )
    pair = pr21g.Pair(pr21g.DEFAULT_TABLE, pr21g.DEFAULT_PREFIX_COLUMNS, pr21g.DEFAULT_COMPOSITE_COLUMNS)
    state = pr21g.dry_run_state(Namespace(schema="public"))
    row = pr21g.build_pair_row(
        pair,
        "hardcoded_default_asserted",
        pr21g.STATUS_READY_FOR_MEASUREMENT,
        state,
        ddl_is_allowed=False,
        repetitions=3,
    )
    summary = pr21g.build_summary_rows(input_audit, row, state)
    manifest = pr21g.manifest(
        input_audit,
        state,
        3,
        {
            "by_pair": tmp_path / "by_pair.csv",
            "summary": tmp_path / "summary.csv",
            "report": tmp_path / "report.md",
        },
    )

    report = pr21g.report_text(manifest, input_audit, row, summary)

    assert "PR21g-1 measures isolated storage and transition evidence only." in report
    assert "It does not measure write-maintenance." in report
    assert "It does not measure online contention." in report
    assert "PR21b-online remains blocked." in report
