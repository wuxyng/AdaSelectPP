from argparse import Namespace
import csv
from pathlib import Path

import pytest

import tools.pr21g_measure_prefix_upgrade_storage_transition as pr21g


class FakeCursor:
    def __init__(self, fetchone_result=(None,)):
        self.statements = []
        self.params = []
        self.fetchone_result = fetchone_result

    def execute(self, sql, params=()):
        self.statements.append(sql)
        self.params.append(tuple(params))

    def fetchone(self):
        return self.fetchone_result


def write_csv(path: Path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


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


def test_drop_and_cleanup_are_schema_qualified_for_non_default_schema():
    cursor = FakeCursor()

    pr21g.drop_pr21g_index(cursor, "bench_schema", "pr21g_composite_movie_info_001")
    pr21g.cleanup_created_indexes(cursor, "bench_schema", ["pr21g_prefix_movie_info_002"])

    assert cursor.statements == [
        'DROP INDEX IF EXISTS "bench_schema"."pr21g_composite_movie_info_001"',
        'DROP INDEX IF EXISTS "bench_schema"."pr21g_prefix_movie_info_002"',
    ]


def test_relation_size_lookup_is_schema_qualified():
    cursor = FakeCursor(fetchone_result=(4096,))

    size = pr21g.relation_size(cursor, "bench_schema", "pr21g_composite_movie_info_001")

    assert size == 4096
    assert cursor.params[0] == (
        '"bench_schema"."pr21g_composite_movie_info_001"',
        '"bench_schema"."pr21g_composite_movie_info_001"',
    )


def test_find_index_casts_attribute_names_to_text_for_catalog_comparison():
    cursor = FakeCursor(fetchone_result=None)

    name, size = pr21g.find_index(cursor, "public", "movie_info", ("mi_movie_id",))

    assert name == ""
    assert size is None
    assert "array_agg(a.attname::text ORDER BY k.ordinality)" in cursor.statements[0]
    assert "= %s::text[]" in cursor.statements[0]
    assert cursor.params[0] == ("public", "movie_info", ["mi_movie_id"])


def test_empty_table_status_maps_to_not_computable_empty_table():
    assert pr21g.empty_table_status(0) == pr21g.STATUS_EMPTY_TABLE
    assert pr21g.empty_table_status(12) == pr21g.STATUS_READY_FOR_MEASUREMENT


def test_storage_delta_ratio_is_explicitly_named_vs_prefix():
    assert "storage_delta_ratio" not in pr21g.BY_PAIR_COLUMNS
    assert "storage_delta_ratio_vs_prefix" in pr21g.BY_PAIR_COLUMNS
    assert "storage_delta_ratio_vs_prefix_evidence_source" in pr21g.BY_PAIR_COLUMNS


def test_no_benefit_payback_roi_net_fields_in_output_schema():
    assert pr21g.forbidden_fields_absent(pr21g.BY_PAIR_COLUMNS)


def test_canonical_pair_is_read_from_pr21e_pair_fields(tmp_path: Path):
    path = tmp_path / "pr21e.csv"
    write_csv(
        path,
        ["operator_check_status", "operator_check_notes", "prefix_index", "composite_index"],
        [{
            "operator_check_status": "operator_eligible",
            "operator_check_notes": "exact_prefix_to_composite_upgrade",
            "prefix_index": "movie_info(mi_movie_id)",
            "composite_index": "movie_info(mi_movie_id,mi_info_type_id)",
        }],
    )

    pair, source, status = pr21g.canonical_pair_from_pr21e(path)

    assert pair == pr21g.Pair(pr21g.DEFAULT_TABLE, pr21g.DEFAULT_PREFIX_COLUMNS, pr21g.DEFAULT_COMPOSITE_COLUMNS)
    assert source == "pr21e_by_round_prefix_composite_fields"
    assert status == pr21g.STATUS_READY_FOR_MEASUREMENT


def test_canonical_pair_fields_missing_is_explicit_fallback(tmp_path: Path):
    path = tmp_path / "pr21e.csv"
    write_csv(
        path,
        ["operator_check_status", "operator_check_notes"],
        [{
            "operator_check_status": "operator_eligible",
            "operator_check_notes": "exact_prefix_to_composite_upgrade",
        }],
    )

    pair, source, status = pr21g.canonical_pair_from_pr21e(path)

    assert pair == pr21g.Pair(pr21g.DEFAULT_TABLE, pr21g.DEFAULT_PREFIX_COLUMNS, pr21g.DEFAULT_COMPOSITE_COLUMNS)
    assert source == "hardcoded_default_pair_fields_missing"
    assert status == pr21g.STATUS_PAIR_FIELDS_MISSING


def test_canonical_pair_multiple_distinct_pairs_fail_loudly(tmp_path: Path):
    path = tmp_path / "pr21e.csv"
    write_csv(
        path,
        ["operator_check_status", "operator_check_notes", "prefix_index", "composite_index"],
        [
            {
                "operator_check_status": "operator_eligible",
                "operator_check_notes": "exact_prefix_to_composite_upgrade",
                "prefix_index": "movie_info(mi_movie_id)",
                "composite_index": "movie_info(mi_movie_id,mi_info_type_id)",
            },
            {
                "operator_check_status": "operator_eligible",
                "operator_check_notes": "exact_prefix_to_composite_upgrade",
                "prefix_index": "cast_info(ci_movie_id)",
                "composite_index": "cast_info(ci_movie_id,ci_person_id)",
            },
        ],
    )

    with pytest.raises(ValueError, match="ambiguous canonical pair set|canonical pair mismatch"):
        pr21g.canonical_pair_from_pr21e(path)


def test_timing_no_sample_is_not_measured_after_successful_db_measurement():
    evidence, scope, status = pr21g.timing_status([], pr21g.STATUS_MEASURED)

    assert evidence == pr21g.EVIDENCE_NOT_COMPUTABLE
    assert scope == pr21g.SCOPE_NOT_COMPUTABLE
    assert status == pr21g.STATUS_NO_TIMING_SAMPLE


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
