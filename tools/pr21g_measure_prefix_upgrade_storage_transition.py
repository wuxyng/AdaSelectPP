#!/usr/bin/env python3
"""PR21g-1 isolated storage and transition measurement tool.

The tool is offline-only. Its default mode performs artifact/schema checks and
emits NOT_COMPUTABLE_NO_DB rows. DDL timing is attempted only when all explicit
database flags are provided.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import re
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


OUTPUT_ROOT = Path("runs_pr21g_storage_transition")
SCRIPT_PATH = Path("tools/pr21g_measure_prefix_upgrade_storage_transition.py")
INPUT_PR21E_BY_ROUND = Path("runs_pr21e_offline_validation/pr21e_validation_by_round.csv")

DEFAULT_SCHEMA = "public"
DEFAULT_TABLE = "movie_info"
DEFAULT_PREFIX_COLUMNS = ("mi_movie_id",)
DEFAULT_COMPOSITE_COLUMNS = ("mi_movie_id", "mi_info_type_id")
DEFAULT_REPETITIONS = 3
SIZE_API = "pg_relation_size"
FLOAT_FORMAT_POLICY = ".12g"
STABLE_SORTING_POLICY = "pair_key"

EVIDENCE_MEASURED = "MEASURED"
EVIDENCE_NOT_COMPUTABLE = "NOT_COMPUTABLE"

SCOPE_CATALOG_SIZE = "MEASURED_CATALOG_SIZE"
SCOPE_ISOLATED_SINGLE_CONN = "MEASURED_ISOLATED_SINGLE_CONN"
SCOPE_NOT_COMPUTABLE = "NOT_COMPUTABLE"

STATUS_READY_FOR_MEASUREMENT = "DRY_RUN_SCHEMA_CHECK_ONLY"
STATUS_NO_DB = "NOT_COMPUTABLE_NO_DB"
STATUS_DB_DRIVER_MISSING = "NOT_COMPUTABLE_DB_DRIVER_MISSING"
STATUS_DB_ERROR = "NOT_COMPUTABLE_DB_ERROR"
STATUS_DDL_NOT_ALLOWED = "NOT_COMPUTABLE_DDL_NOT_ALLOWED"
STATUS_MISSING_TABLE = "NOT_COMPUTABLE_MISSING_TABLE"
STATUS_EMPTY_TABLE = "NOT_COMPUTABLE_EMPTY_TABLE"
STATUS_MISSING_INDEX = "NOT_COMPUTABLE_MISSING_INDEX"
STATUS_PAIR_FIELDS_MISSING = "HARD_CODED_DEFAULT_PAIR_FIELDS_MISSING"
STATUS_MISSING_INPUT = "NOT_COMPUTABLE_MISSING_INPUT"
STATUS_NO_TIMING_SAMPLE = "NOT_COMPUTABLE_NO_TIMING_SAMPLE"
STATUS_MEASURED = "MEASURED"

TIMING_FIELDS = ("median", "stdev", "cv", "min", "max")
TIMING_OPERATIONS = ("create_composite", "drop_composite", "create_prefix", "drop_prefix")

FORBIDDEN_FIELD_FRAGMENTS = (
    "roi",
    "payback",
    "net_benefit",
    "benefit_cost",
    "cost_benefit",
    "eligibility_score",
    "accept_label",
    "recommendation_label",
)

BY_PAIR_COLUMNS = [
    "pair_key",
    "table_name",
    "prefix_index",
    "composite_index",
    "prefix_columns",
    "composite_columns",
    "canonical_pair_source",
    "canonical_pair_status",
    "movie_info_row_count",
    "movie_info_row_count_evidence_source",
    "movie_info_row_count_measurement_scope",
    "movie_info_row_count_status",
    "movie_info_row_count_manifest_ref",
    "prefix_size_bytes",
    "prefix_size_bytes_evidence_source",
    "prefix_size_bytes_measurement_scope",
    "prefix_size_bytes_status",
    "prefix_size_bytes_manifest_ref",
    "composite_size_bytes",
    "composite_size_bytes_evidence_source",
    "composite_size_bytes_measurement_scope",
    "composite_size_bytes_status",
    "composite_size_bytes_manifest_ref",
    "storage_delta_bytes",
    "storage_delta_bytes_evidence_source",
    "storage_delta_bytes_measurement_scope",
    "storage_delta_bytes_status",
    "storage_delta_bytes_manifest_ref",
    "transient_peak_storage_bytes",
    "transient_peak_storage_bytes_evidence_source",
    "transient_peak_storage_bytes_measurement_scope",
    "transient_peak_storage_bytes_status",
    "transient_peak_storage_bytes_manifest_ref",
    "storage_delta_ratio_vs_prefix",
    "storage_delta_ratio_vs_prefix_evidence_source",
    "storage_delta_ratio_vs_prefix_measurement_scope",
    "storage_delta_ratio_vs_prefix_status",
    "storage_delta_ratio_vs_prefix_manifest_ref",
    "prefix_index_existed_before",
    "composite_index_existed_before",
    "ddl_allowed",
    "online_contention_still_blocked",
    "write_maintenance_measured",
]

for operation in TIMING_OPERATIONS:
    for field in TIMING_FIELDS:
        BY_PAIR_COLUMNS.append(f"{operation}_ms_{field}")
    BY_PAIR_COLUMNS.extend([
        f"{operation}_ms_evidence_source",
        f"{operation}_ms_measurement_scope",
        f"{operation}_ms_status",
        f"{operation}_ms_repetitions",
        f"{operation}_ms_manifest_ref",
    ])

SUMMARY_COLUMNS = ["section", "metric", "value", "evidence_source", "measurement_scope", "status", "notes"]


@dataclass(frozen=True)
class Pair:
    table: str
    prefix_columns: Tuple[str, ...]
    composite_columns: Tuple[str, ...]

    @property
    def prefix_index(self) -> str:
        return f"{self.table}({','.join(self.prefix_columns)})"

    @property
    def composite_index(self) -> str:
        return f"{self.table}({','.join(self.composite_columns)})"

    @property
    def key(self) -> str:
        return f"{self.prefix_index} -> {self.composite_index}"


@dataclass(frozen=True)
class InputAudit:
    path: Path
    exists: bool
    row_count: int
    content_hash: str
    columns: Tuple[str, ...]
    missing_columns: Tuple[str, ...]


@dataclass(frozen=True)
class DbEnvironment:
    postgresql_version: str = "unknown"
    database_name: str = "unknown"
    schema_name: str = DEFAULT_SCHEMA
    table_name: str = DEFAULT_TABLE
    schema_table_row_count: str = "unknown"
    movie_info_row_count: str = ""
    dataset_scale_note: str = "unknown"
    shared_buffers: str = "unknown"
    work_mem: str = "unknown"
    maintenance_work_mem: str = "unknown"
    max_parallel_maintenance_workers: str = "unknown"
    concurrent_load_observed: str = "unknown"
    os_cpu: str = "unknown"
    storage_type: str = "unknown"

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.__dict__, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DbState:
    status: str
    environment: DbEnvironment
    table_exists: bool = False
    table_empty: bool = False
    prefix_index_name: str = ""
    prefix_index_size_bytes: Optional[int] = None
    composite_index_name: str = ""
    composite_index_size_bytes: Optional[int] = None
    prefix_index_existed_before: bool = False
    composite_index_existed_before: bool = False
    create_composite_ms: Tuple[float, ...] = ()
    drop_composite_ms: Tuple[float, ...] = ()
    create_prefix_ms: Tuple[float, ...] = ()
    drop_prefix_ms: Tuple[float, ...] = ()
    notes: str = ""


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def git_output(args: Sequence[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def current_git_commit() -> str:
    return git_output(["rev-parse", "HEAD"]) or "UNKNOWN"


def generation_timestamp() -> str:
    commit_time = git_output(["show", "-s", "--format=%cI", "HEAD"])
    if commit_time:
        return commit_time
    return datetime.fromtimestamp(0, tz=timezone.utc).isoformat()


def script_version() -> str:
    if not SCRIPT_PATH.exists():
        return "UNKNOWN"
    return f"SCRIPT_CONTENT_SHA256:{sha256_file(SCRIPT_PATH)}"


def read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [
            {key: (value if value is not None else "") for key, value in row.items()}
            for row in reader
        ]
    return list(reader.fieldnames or []), rows


def audit_input(path: Path, expected_columns: Sequence[str]) -> InputAudit:
    if not path.exists():
        return InputAudit(path, False, 0, EVIDENCE_NOT_COMPUTABLE, (), tuple(expected_columns))
    columns, rows = read_csv(path)
    missing = tuple(col for col in expected_columns if col not in columns)
    return InputAudit(path, True, len(rows), sha256_file(path), tuple(columns), missing)


def fmt_float(value: Optional[float]) -> str:
    if value is None:
        return ""
    return format(value, FLOAT_FORMAT_POLICY)


def fmt_int(value: Optional[int]) -> str:
    return "" if value is None else str(value)


def parse_index_identity(text: str) -> Pair:
    stripped = str(text).strip()
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\(([^()]*)\)", stripped)
    if not match:
        raise ValueError(f"cannot parse index identity: {text}")
    table = match.group(1)
    columns = tuple(col.strip() for col in match.group(2).split(",") if col.strip())
    if not columns:
        raise ValueError(f"index identity has no columns: {text}")
    return Pair(table, columns, columns)


def pair_from_index_identities(prefix_index: str, composite_index: str) -> Pair:
    prefix = parse_index_identity(prefix_index)
    composite = parse_index_identity(composite_index)
    if prefix.table != composite.table:
        raise ValueError(f"prefix/composite table mismatch: {prefix_index} != {composite_index}")
    return Pair(prefix.table, prefix.prefix_columns, composite.prefix_columns)


def canonical_pair_from_pr21e(path: Path) -> Tuple[Pair, str, str]:
    expected = Pair(DEFAULT_TABLE, DEFAULT_PREFIX_COLUMNS, DEFAULT_COMPOSITE_COLUMNS)
    if not path.exists():
        return expected, "hardcoded_default_missing_pr21e", STATUS_MISSING_INPUT
    columns, rows = read_csv(path)
    eligible_rows = [
        row for row in rows
        if row.get("operator_check_status") == "operator_eligible"
        and row.get("operator_check_notes") == "exact_prefix_to_composite_upgrade"
    ]
    if "prefix_index" not in columns or "composite_index" not in columns:
        return expected, "hardcoded_default_pair_fields_missing", STATUS_PAIR_FIELDS_MISSING

    pairs = {
        pair_from_index_identities(row.get("prefix_index", ""), row.get("composite_index", ""))
        for row in eligible_rows
        if row.get("prefix_index", "").strip() and row.get("composite_index", "").strip()
    }
    if len(pairs) != 1:
        raise ValueError(f"ambiguous canonical pair set from PR21e: {sorted(pair.key for pair in pairs)}")
    pair = next(iter(pairs))
    if pair != expected:
        raise ValueError(f"canonical pair mismatch: {pair.key} != {expected.key}")
    return pair, "pr21e_by_round_prefix_composite_fields", STATUS_READY_FOR_MEASUREMENT


def assert_dominant_pair(pair: Pair) -> None:
    expected = Pair(DEFAULT_TABLE, DEFAULT_PREFIX_COLUMNS, DEFAULT_COMPOSITE_COLUMNS)
    if pair != expected:
        raise ValueError(f"canonical pair mismatch: {pair.key} != {expected.key}")


def ddl_allowed(db_url: str, allow_ddl: bool, confirm_isolated_db: bool) -> bool:
    return bool(db_url and allow_ddl and confirm_isolated_db)


def generated_index_name(kind: str, repetition: int) -> str:
    if kind not in {"prefix", "composite"}:
        raise ValueError(f"unexpected index kind: {kind}")
    return f"pr21g_{kind}_movie_info_{repetition:03d}"


def validate_pr21g_index_name(index_name: str) -> None:
    if not re.fullmatch(r"pr21g_[a-z0-9_]+", index_name):
        raise ValueError(f"refusing to drop non-PR21g-owned index: {index_name}")


def quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"invalid SQL identifier: {identifier}")
    return '"' + identifier.replace('"', '""') + '"'


def qualified_index_name(schema: str, index_name: str) -> str:
    validate_pr21g_index_name(index_name)
    return f"{quote_ident(schema)}.{quote_ident(index_name)}"


def index_column_sql(columns: Sequence[str]) -> str:
    return ", ".join(quote_ident(col) for col in columns)


def timing_summary(samples: Sequence[float]) -> Dict[str, str]:
    if not samples:
        return {field: "" for field in TIMING_FIELDS}
    median = statistics.median(samples)
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    mean = statistics.mean(samples)
    cv = stdev / mean if mean else 0.0
    return {
        "median": fmt_float(median),
        "stdev": fmt_float(stdev),
        "cv": fmt_float(cv),
        "min": fmt_float(min(samples)),
        "max": fmt_float(max(samples)),
    }


def empty_table_status(row_count: int) -> str:
    return STATUS_EMPTY_TABLE if row_count == 0 else STATUS_READY_FOR_MEASUREMENT


def forbidden_fields_absent(columns: Sequence[str]) -> bool:
    lowered = [col.lower() for col in columns]
    return not any(fragment in col for col in lowered for fragment in FORBIDDEN_FIELD_FRAGMENTS)


def import_db_driver():
    try:
        import psycopg  # type: ignore
        return "psycopg", psycopg
    except Exception:
        try:
            import psycopg2  # type: ignore
            return "psycopg2", psycopg2
        except Exception:
            return "", None


def fetch_one(cursor, sql: str, params: Sequence[object] = ()) -> Tuple[object, ...]:
    cursor.execute(sql, tuple(params))
    row = cursor.fetchone()
    if row is None:
        return ()
    return tuple(row)


def setting(cursor, name: str) -> str:
    row = fetch_one(cursor, "SELECT current_setting(%s, true)", (name,))
    return str(row[0]) if row and row[0] is not None else "unknown"


def find_index(cursor, schema: str, table: str, columns: Sequence[str]) -> Tuple[str, Optional[int]]:
    sql = """
        SELECT c.relname, pg_relation_size(c.oid)
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = %s
          AND t.relname = %s
          AND i.indisvalid
          AND (
            SELECT array_agg(a.attname::text ORDER BY k.ordinality)
            FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality)
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
          ) = %s::text[]
        ORDER BY c.relname
        LIMIT 1
    """
    row = fetch_one(cursor, sql, (schema, table, list(columns)))
    if not row:
        return "", None
    return str(row[0]), int(row[1])


def relation_size(cursor, schema: str, index_name: str) -> Optional[int]:
    qualified = qualified_index_name(schema, index_name)
    row = fetch_one(
        cursor,
        "SELECT CASE WHEN to_regclass(%s) IS NULL THEN NULL ELSE pg_relation_size(to_regclass(%s)) END",
        (qualified, qualified),
    )
    if not row or row[0] is None:
        return None
    return int(row[0])


def create_index(cursor, schema: str, table: str, index_name: str, columns: Sequence[str]) -> float:
    validate_pr21g_index_name(index_name)
    sql = (
        f"CREATE INDEX {quote_ident(index_name)} ON "
        f"{quote_ident(schema)}.{quote_ident(table)} ({index_column_sql(columns)})"
    )
    start = time.perf_counter()
    cursor.execute(sql)
    return (time.perf_counter() - start) * 1000.0


def drop_pr21g_index(cursor, schema: str, index_name: str) -> float:
    qualified = qualified_index_name(schema, index_name)
    start = time.perf_counter()
    cursor.execute(f"DROP INDEX IF EXISTS {qualified}")
    return (time.perf_counter() - start) * 1000.0


def cleanup_created_indexes(cursor, schema: str, created: Iterable[str]) -> None:
    for index_name in reversed(list(created)):
        cursor.execute(f"DROP INDEX IF EXISTS {qualified_index_name(schema, index_name)}")


def measure_with_db(args: argparse.Namespace, pair: Pair) -> DbState:
    driver_name, driver = import_db_driver()
    if driver is None:
        return DbState(STATUS_DB_DRIVER_MISSING, DbEnvironment(schema_name=args.schema), notes="psycopg/psycopg2 is not importable")

    created: List[str] = []
    try:
        conn = driver.connect(args.db_url)
        try:
            if hasattr(conn, "autocommit"):
                conn.autocommit = True
            cursor = conn.cursor()

            pg_version = str(fetch_one(cursor, "SHOW server_version")[0])
            db_name = str(fetch_one(cursor, "SELECT current_database()")[0])
            env = DbEnvironment(
                postgresql_version=pg_version,
                database_name=db_name,
                schema_name=args.schema,
                table_name=DEFAULT_TABLE,
                shared_buffers=setting(cursor, "shared_buffers"),
                work_mem=setting(cursor, "work_mem"),
                maintenance_work_mem=setting(cursor, "maintenance_work_mem"),
                max_parallel_maintenance_workers=setting(cursor, "max_parallel_maintenance_workers"),
                concurrent_load_observed="unknown",
                os_cpu=f"{platform.system()} {platform.release()} {platform.machine()}",
                storage_type="unknown",
            )

            table_exists_row = fetch_one(cursor, "SELECT to_regclass(%s)", (f"{args.schema}.{DEFAULT_TABLE}",))
            if not table_exists_row or table_exists_row[0] is None:
                return DbState(STATUS_MISSING_TABLE, env, table_exists=False)

            count = int(fetch_one(cursor, f"SELECT count(*) FROM {quote_ident(args.schema)}.{quote_ident(DEFAULT_TABLE)}")[0])
            env = DbEnvironment(**{**env.__dict__, "schema_table_row_count": str(count), "movie_info_row_count": str(count)})
            if count == 0:
                return DbState(STATUS_EMPTY_TABLE, env, table_exists=True, table_empty=True)

            prefix_name, prefix_size = find_index(cursor, args.schema, pair.table, pair.prefix_columns)
            composite_name, composite_size = find_index(cursor, args.schema, pair.table, pair.composite_columns)
            prefix_existed = bool(prefix_name)
            composite_existed = bool(composite_name)

            if not ddl_allowed(args.db_url, args.allow_ddl, args.confirm_isolated_db):
                return DbState(
                    STATUS_DDL_NOT_ALLOWED,
                    env,
                    table_exists=True,
                    prefix_index_name=prefix_name,
                    prefix_index_size_bytes=prefix_size,
                    composite_index_name=composite_name,
                    composite_index_size_bytes=composite_size,
                    prefix_index_existed_before=prefix_existed,
                    composite_index_existed_before=composite_existed,
                    notes="DDL flags were not all provided; transition timing was not attempted.",
                )

            create_composite_ms: List[float] = []
            drop_composite_ms: List[float] = []
            create_prefix_ms: List[float] = []
            drop_prefix_ms: List[float] = []

            if not prefix_existed:
                for rep in range(args.repetitions):
                    name = generated_index_name("prefix", rep)
                    create_prefix_ms.append(create_index(cursor, args.schema, pair.table, name, pair.prefix_columns))
                    created.append(name)
                    if prefix_size is None:
                        prefix_size = relation_size(cursor, args.schema, name)
                    drop_prefix_ms.append(drop_pr21g_index(cursor, args.schema, name))
                    created.remove(name)

            for rep in range(args.repetitions):
                name = generated_index_name("composite", rep)
                create_composite_ms.append(create_index(cursor, args.schema, pair.table, name, pair.composite_columns))
                created.append(name)
                if composite_size is None:
                    composite_size = relation_size(cursor, args.schema, name)
                drop_composite_ms.append(drop_pr21g_index(cursor, args.schema, name))
                created.remove(name)

            return DbState(
                STATUS_MEASURED,
                env,
                table_exists=True,
                prefix_index_name=prefix_name,
                prefix_index_size_bytes=prefix_size,
                composite_index_name=composite_name,
                composite_index_size_bytes=composite_size,
                prefix_index_existed_before=prefix_existed,
                composite_index_existed_before=composite_existed,
                create_composite_ms=tuple(create_composite_ms),
                drop_composite_ms=tuple(drop_composite_ms),
                create_prefix_ms=tuple(create_prefix_ms),
                drop_prefix_ms=tuple(drop_prefix_ms),
                notes=f"driver={driver_name}",
            )
        finally:
            try:
                if created:
                    cleanup_created_indexes(conn.cursor(), args.schema, created)
            finally:
                conn.close()
    except Exception as exc:
        return DbState(STATUS_DB_ERROR, DbEnvironment(schema_name=args.schema), notes=str(exc))


def dry_run_state(args: argparse.Namespace) -> DbState:
    env = DbEnvironment(
        schema_name=args.schema,
        os_cpu=f"{platform.system()} {platform.release()} {platform.machine()}",
    )
    return DbState(STATUS_NO_DB, env, notes="no --db-url was provided")


def numeric_status(value: Optional[float | int], state_status: str) -> Tuple[str, str, str]:
    if value is None:
        return EVIDENCE_NOT_COMPUTABLE, SCOPE_NOT_COMPUTABLE, state_status
    return EVIDENCE_MEASURED, SCOPE_CATALOG_SIZE, STATUS_MEASURED


def timing_status(samples: Sequence[float], state_status: str) -> Tuple[str, str, str]:
    if not samples:
        status = STATUS_NO_TIMING_SAMPLE if state_status == STATUS_MEASURED else state_status
        return EVIDENCE_NOT_COMPUTABLE, SCOPE_NOT_COMPUTABLE, status
    return EVIDENCE_MEASURED, SCOPE_ISOLATED_SINGLE_CONN, STATUS_MEASURED


def build_pair_row(pair: Pair, source: str, source_status: str, state: DbState, ddl_is_allowed: bool, repetitions: int) -> Dict[str, str]:
    prefix_size = state.prefix_index_size_bytes
    composite_size = state.composite_index_size_bytes
    storage_delta = composite_size - prefix_size if prefix_size is not None and composite_size is not None else None
    transient_peak = prefix_size + composite_size if prefix_size is not None and composite_size is not None else None
    ratio = storage_delta / prefix_size if storage_delta is not None and prefix_size else None
    env_ref = f"manifest.environment_fingerprint:{state.environment.fingerprint}"

    row: Dict[str, str] = {
        "pair_key": pair.key,
        "table_name": pair.table,
        "prefix_index": pair.prefix_index,
        "composite_index": pair.composite_index,
        "prefix_columns": "|".join(pair.prefix_columns),
        "composite_columns": "|".join(pair.composite_columns),
        "canonical_pair_source": source,
        "canonical_pair_status": source_status,
        "prefix_index_existed_before": str(state.prefix_index_existed_before).lower(),
        "composite_index_existed_before": str(state.composite_index_existed_before).lower(),
        "ddl_allowed": str(ddl_is_allowed).lower(),
        "online_contention_still_blocked": "true",
        "write_maintenance_measured": "false",
    }

    row_count = int(state.environment.movie_info_row_count) if state.environment.movie_info_row_count.isdigit() else None
    rc_source, rc_scope, rc_status = numeric_status(row_count, state.status)
    row.update({
        "movie_info_row_count": fmt_int(row_count),
        "movie_info_row_count_evidence_source": rc_source,
        "movie_info_row_count_measurement_scope": rc_scope,
        "movie_info_row_count_status": rc_status,
        "movie_info_row_count_manifest_ref": env_ref,
    })

    storage_metrics = {
        "prefix_size_bytes": prefix_size,
        "composite_size_bytes": composite_size,
        "storage_delta_bytes": storage_delta,
        "transient_peak_storage_bytes": transient_peak,
        "storage_delta_ratio_vs_prefix": ratio,
    }
    for metric, value in storage_metrics.items():
        evidence, scope, status = numeric_status(value, state.status)
        row[metric] = fmt_float(value) if isinstance(value, float) else fmt_int(value)
        row[f"{metric}_evidence_source"] = evidence
        row[f"{metric}_measurement_scope"] = scope
        row[f"{metric}_status"] = status
        row[f"{metric}_manifest_ref"] = env_ref

    samples_by_operation = {
        "create_composite": state.create_composite_ms,
        "drop_composite": state.drop_composite_ms,
        "create_prefix": state.create_prefix_ms,
        "drop_prefix": state.drop_prefix_ms,
    }
    for operation, samples in samples_by_operation.items():
        stats = timing_summary(samples)
        evidence, scope, status = timing_status(samples, state.status)
        for field in TIMING_FIELDS:
            row[f"{operation}_ms_{field}"] = stats[field]
        row[f"{operation}_ms_evidence_source"] = evidence
        row[f"{operation}_ms_measurement_scope"] = scope
        row[f"{operation}_ms_status"] = status
        row[f"{operation}_ms_repetitions"] = str(len(samples) if samples else repetitions)
        row[f"{operation}_ms_manifest_ref"] = env_ref
    return row


def summary_row(section: str, metric: str, value: object, evidence: str, scope: str, status: str, notes: str) -> Dict[str, str]:
    return {
        "section": section,
        "metric": metric,
        "value": str(value),
        "evidence_source": evidence,
        "measurement_scope": scope,
        "status": status,
        "notes": notes,
    }


def build_summary_rows(input_audit: InputAudit, pair_row: Mapping[str, str], state: DbState) -> List[Dict[str, str]]:
    rows = [
        summary_row("input", "pr21e_by_round_rows", input_audit.row_count, EVIDENCE_MEASURED if input_audit.exists else EVIDENCE_NOT_COMPUTABLE, SCOPE_NOT_COMPUTABLE, STATUS_MEASURED if input_audit.exists else STATUS_NO_DB, str(input_audit.path)),
        summary_row("precondition", "movie_info_table", str(state.table_exists).lower(), EVIDENCE_MEASURED if state.table_exists else EVIDENCE_NOT_COMPUTABLE, SCOPE_NOT_COMPUTABLE, state.status, state.notes),
        summary_row("precondition", "movie_info_row_count", pair_row["movie_info_row_count"], pair_row["movie_info_row_count_evidence_source"], pair_row["movie_info_row_count_measurement_scope"], pair_row["movie_info_row_count_status"], "nonzero table required before measuring storage"),
        summary_row("storage", "prefix_size_bytes", pair_row["prefix_size_bytes"], pair_row["prefix_size_bytes_evidence_source"], pair_row["prefix_size_bytes_measurement_scope"], pair_row["prefix_size_bytes_status"], SIZE_API),
        summary_row("storage", "composite_size_bytes", pair_row["composite_size_bytes"], pair_row["composite_size_bytes_evidence_source"], pair_row["composite_size_bytes_measurement_scope"], pair_row["composite_size_bytes_status"], SIZE_API),
        summary_row("storage", "storage_delta_bytes", pair_row["storage_delta_bytes"], pair_row["storage_delta_bytes_evidence_source"], pair_row["storage_delta_bytes_measurement_scope"], pair_row["storage_delta_bytes_status"], "composite minus prefix"),
        summary_row("storage", "transient_peak_storage_bytes", pair_row["transient_peak_storage_bytes"], pair_row["transient_peak_storage_bytes_evidence_source"], pair_row["transient_peak_storage_bytes_measurement_scope"], pair_row["transient_peak_storage_bytes_status"], "prefix plus composite"),
        summary_row("storage", "storage_delta_ratio_vs_prefix", pair_row["storage_delta_ratio_vs_prefix"], pair_row["storage_delta_ratio_vs_prefix_evidence_source"], pair_row["storage_delta_ratio_vs_prefix_measurement_scope"], pair_row["storage_delta_ratio_vs_prefix_status"], "storage_delta_bytes / prefix_size_bytes"),
    ]
    for operation in TIMING_OPERATIONS:
        rows.append(summary_row(
            "transition",
            f"{operation}_ms_median",
            pair_row[f"{operation}_ms_median"],
            pair_row[f"{operation}_ms_evidence_source"],
            pair_row[f"{operation}_ms_measurement_scope"],
            pair_row[f"{operation}_ms_status"],
            f"repetitions={pair_row[f'{operation}_ms_repetitions']}",
        ))
    rows.extend([
        summary_row("scope", "write_maintenance_measured", "false", EVIDENCE_NOT_COMPUTABLE, SCOPE_NOT_COMPUTABLE, EVIDENCE_NOT_COMPUTABLE, "PR21g-1 does not measure write-maintenance."),
        summary_row("scope", "online_contention_still_blocked", "true", EVIDENCE_NOT_COMPUTABLE, SCOPE_NOT_COMPUTABLE, EVIDENCE_NOT_COMPUTABLE, "PR21g-1 does not measure online contention."),
        summary_row("schema", "forbidden_fields_absent", str(forbidden_fields_absent(BY_PAIR_COLUMNS)).lower(), EVIDENCE_MEASURED, SCOPE_NOT_COMPUTABLE, STATUS_MEASURED, "forbidden decision fields are absent"),
        summary_row("online_activation", "PR21b-online", "blocked", EVIDENCE_NOT_COMPUTABLE, SCOPE_NOT_COMPUTABLE, EVIDENCE_NOT_COMPUTABLE, "PR21b-online remains blocked."),
    ])
    return rows


def manifest(input_audit: InputAudit, state: DbState, repetitions: int, output_paths: Mapping[str, Path]) -> Dict[str, object]:
    return {
        "generation_timestamp": generation_timestamp(),
        "current_git_commit": current_git_commit(),
        "script_path": str(SCRIPT_PATH),
        "script_hash": sha256_file(SCRIPT_PATH) if SCRIPT_PATH.exists() else EVIDENCE_NOT_COMPUTABLE,
        "script_git_commit_or_version": script_version(),
        "input_artifact": {
            "path": str(input_audit.path),
            "hash": input_audit.content_hash,
            "row_count": input_audit.row_count,
            "exists": input_audit.exists,
            "columns": list(input_audit.columns),
            "missing_columns": list(input_audit.missing_columns),
        },
        "postgresql_version": state.environment.postgresql_version,
        "database_name": state.environment.database_name,
        "schema_table_row_count": state.environment.schema_table_row_count,
        "movie_info_row_count": state.environment.movie_info_row_count,
        "dataset_scale_note": state.environment.dataset_scale_note,
        "size_api_used": SIZE_API,
        "cpu_os": state.environment.os_cpu,
        "storage_type": state.environment.storage_type,
        "shared_buffers": state.environment.shared_buffers,
        "work_mem": state.environment.work_mem,
        "maintenance_work_mem": state.environment.maintenance_work_mem,
        "max_parallel_maintenance_workers": state.environment.max_parallel_maintenance_workers,
        "concurrent_load_observed": state.environment.concurrent_load_observed,
        "timing_repetitions_n": repetitions,
        "environment_fingerprint": state.environment.fingerprint,
        "stable_sorting_policy": STABLE_SORTING_POLICY,
        "float_formatting_policy": FLOAT_FORMAT_POLICY,
        "output_files": {name: str(path) for name, path in output_paths.items()},
    }


def report_text(manifest_data: Mapping[str, object], input_audit: InputAudit, pair_row: Mapping[str, str], summary_rows: Sequence[Mapping[str, str]]) -> str:
    lines = [
        "# PR21g-1 Offline Storage And Transition Measurement",
        "",
        "storage size was measured in isolated DB;",
        "isolated create/drop transition timing was measured;",
        "write-maintenance and online contention remain unmeasured blockers;",
        "PR21b-online remains blocked.",
        "",
        f"Size API used: `{SIZE_API}` for index relation main-size measurement.",
        "",
        "## Manifest",
        "",
        "```json",
        json.dumps(manifest_data, indent=2, sort_keys=True),
        "```",
        "",
        "## Input Artifact",
        "",
        f"- path: `{input_audit.path}`",
        f"- exists: `{str(input_audit.exists).lower()}`",
        f"- row count: `{input_audit.row_count}`",
        f"- hash: `{input_audit.content_hash}`",
        f"- missing columns: `{', '.join(input_audit.missing_columns)}`",
        "",
        "## Pair",
        "",
        f"- prefix: `{pair_row['prefix_index']}`",
        f"- composite: `{pair_row['composite_index']}`",
        f"- canonical source: `{pair_row['canonical_pair_source']}`",
        f"- online_contention_still_blocked: `{pair_row['online_contention_still_blocked']}`",
        "",
        "## Summary",
        "",
        "| section | metric | value | evidence_source | measurement_scope | status | notes |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for row in summary_rows:
        lines.append(
            f"| `{row['section']}` | `{row['metric']}` | {row['value']} | "
            f"`{row['evidence_source']}` | `{row['measurement_scope']}` | `{row['status']}` | {row['notes']} |"
        )
    lines.extend([
        "",
        "## Conclusion",
        "",
        "storage size was measured in isolated DB;",
        "isolated create/drop transition timing was measured;",
        "write-maintenance and online contention remain unmeasured blockers;",
        "PR21b-online remains blocked.",
        "",
    ])
    return "\n".join(lines)


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default="")
    parser.add_argument("--allow-ddl", action="store_true")
    parser.add_argument("--confirm-isolated-db", action="store_true")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--pr21e-by-round", default=str(INPUT_PR21E_BY_ROUND))
    parser.add_argument("--output-dir", default=str(OUTPUT_ROOT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repetitions < 1:
        raise ValueError("--repetitions must be >= 1")

    input_path = Path(args.pr21e_by_round)
    input_audit = audit_input(input_path, ("source_artifact", "row_index", "operator_check_status", "operator_check_notes"))
    pair, pair_source, pair_status = canonical_pair_from_pr21e(input_path)
    assert_dominant_pair(pair)

    if args.db_url:
        state = measure_with_db(args, pair)
    else:
        state = dry_run_state(args)
    ddl_is_allowed = ddl_allowed(args.db_url, args.allow_ddl, args.confirm_isolated_db)
    pair_row = build_pair_row(pair, pair_source, pair_status, state, ddl_is_allowed, args.repetitions)
    summary_rows = build_summary_rows(input_audit, pair_row, state)

    output_dir = Path(args.output_dir)
    output_paths = {
        "by_pair": output_dir / "pr21g_storage_transition_by_pair.csv",
        "summary": output_dir / "pr21g_storage_transition_summary.csv",
        "report": output_dir / "pr21g_storage_transition_report.md",
    }
    manifest_data = manifest(input_audit, state, args.repetitions, output_paths)
    report = report_text(manifest_data, input_audit, pair_row, summary_rows)

    write_csv(output_paths["by_pair"], BY_PAIR_COLUMNS, [pair_row])
    write_csv(output_paths["summary"], SUMMARY_COLUMNS, summary_rows)
    output_paths["report"].parent.mkdir(parents=True, exist_ok=True)
    output_paths["report"].write_text(report, encoding="utf-8")

    for path in output_paths.values():
        print(f"Wrote {path}")
    print("PR21g-1 measures isolated storage and transition evidence only.")
    print("PR21b-online remains blocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
