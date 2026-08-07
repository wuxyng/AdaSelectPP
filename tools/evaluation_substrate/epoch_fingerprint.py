"""Fail-closed PostgreSQL optimizer epoch fingerprinting.

This module deliberately accepts an already-open DB-API connection.  It does
not import AdaSelectPP's online database connector and it never performs DDL.
The captured payload contains every database fact that Evaluation Substrate v0
uses to decide whether an optimizer response may be reused.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Tuple


EPOCH_SCHEMA_VERSION = "evaluation_substrate_epoch_v0.1"

# Settings which must exist on every supported server.  The list is explicit:
# hashing all pg_settings would make the equivalence claim both unauditable and
# unnecessarily sensitive to settings which cannot affect planning.
PLANNER_GUCS: Tuple[str, ...] = (
    "geqo",
    "geqo_threshold",
    "geqo_effort",
    "geqo_pool_size",
    "geqo_generations",
    "geqo_selection_bias",
    "geqo_seed",
    "join_collapse_limit",
    "from_collapse_limit",
    "max_parallel_workers_per_gather",
    "min_parallel_table_scan_size",
    "min_parallel_index_scan_size",
    "seq_page_cost",
    "random_page_cost",
    "cpu_tuple_cost",
    "cpu_index_tuple_cost",
    "cpu_operator_cost",
    "parallel_setup_cost",
    "parallel_tuple_cost",
    "effective_cache_size",
    "effective_io_concurrency",
    "work_mem",
    "cursor_tuple_fraction",
    "constraint_exclusion",
    "row_security",
    "max_parallel_workers",
    "max_worker_processes",
    "default_statistics_target",
    "default_text_search_config",
    "DateStyle",
    "IntervalStyle",
    "TimeZone",
    "transform_null_equals",
)

# These settings were introduced in different PostgreSQL releases.  Their
# availability is itself part of the epoch, so an unsupported setting is
# recorded explicitly instead of being silently omitted.
OPTIONAL_PLANNER_GUCS: Tuple[str, ...] = (
    "hash_mem_multiplier",
    "plan_cache_mode",
    "parallel_leader_participation",
    "jit",
    "jit_above_cost",
    "jit_inline_above_cost",
    "jit_optimize_above_cost",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EpochFingerprintError(RuntimeError):
    """Raised when a complete, trustworthy optimizer epoch cannot be read."""


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical JSON object keys must be strings, got {key!r}")
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [_canonical_value(item) for item in value]
        return sorted(converted, key=lambda item: canonical_json(item))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot enter a fingerprint")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float cannot enter a fingerprint")
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize *value* to the single canonical JSON form used for hashes."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path | str, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a regular file without loading it into memory."""

    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"input artifact is not a regular file: {file_path}")
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _query_all(connection: Any, statement: str) -> list[Any]:
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(statement)
        return list(cursor.fetchall())
    except Exception as exc:
        raise EpochFingerprintError(f"epoch query failed: {exc}") from exc
    finally:
        if cursor is not None:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()


def _values(row: Any, columns: Sequence[str]) -> list[Any]:
    if isinstance(row, Mapping):
        try:
            values = [row[column] for column in columns]
        except KeyError as exc:
            raise EpochFingerprintError(f"epoch row is missing column {exc.args[0]!r}") from exc
    else:
        try:
            values = list(row)
        except TypeError as exc:
            raise EpochFingerprintError(f"epoch query returned a non-row value: {row!r}") from exc
        if len(values) != len(columns):
            raise EpochFingerprintError(
                f"epoch query returned {len(values)} columns; expected {len(columns)}"
            )
    return [_canonical_value(value) for value in values]


def _one_row(rows: Sequence[Any], columns: Sequence[str], *, label: str) -> list[Any]:
    if len(rows) != 1:
        raise EpochFingerprintError(f"expected one {label} row, found {len(rows)}")
    return _values(rows[0], columns)


def _normalize_relevant_relations(
    relevant_relations: Optional[Iterable[object]],
) -> Optional[Tuple[Tuple[str, str], ...]]:
    if relevant_relations is None:
        return None
    normalized: set[Tuple[str, str]] = set()
    for relation in relevant_relations:
        schema: object
        table: object
        if isinstance(relation, str):
            text = relation.strip()
            if "." in text:
                schema, table = text.split(".", 1)
            else:
                schema, table = "public", text
        elif isinstance(relation, Mapping):
            schema = relation.get("schema", relation.get("schemaname", "public"))
            table = relation.get("table", relation.get("tablename", ""))
        else:
            try:
                schema, table = relation  # type: ignore[misc]
            except Exception as exc:
                raise ValueError(
                    "relevant relations must be 'schema.table', (schema, table), or mappings"
                ) from exc
        schema_text = str(schema).strip()
        table_text = str(table).strip()
        if not schema_text or not table_text or "\x00" in schema_text or "\x00" in table_text:
            raise ValueError(f"invalid relevant relation: {relation!r}")
        normalized.add((schema_text, table_text))
    if not normalized:
        raise ValueError("relevant_relations cannot be empty; use None for all user relations")
    return tuple(sorted(normalized))


def _filter_rows(
    rows: Sequence[Any],
    columns: Sequence[str],
    relevant: Optional[Tuple[Tuple[str, str], ...]],
) -> list[list[Any]]:
    parsed = [_values(row, columns) for row in rows]
    if relevant is not None:
        allowed = set(relevant)
        parsed = [row for row in parsed if (str(row[0]), str(row[1])) in allowed]
    return sorted(parsed, key=canonical_json)


def _table_fingerprint(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict[str, Any]:
    payload = {"columns": list(columns), "rows": list(rows)}
    return {
        "sha256": canonical_sha256(payload),
        "row_count": len(rows),
    }


def _epoch_hash_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    scientific_keys = (
        "epoch_schema_version",
        "relevant_relations",
        "database_environment",
        "statistics_fingerprint",
        "schema_fingerprint",
        "physical_index_fingerprint",
    )
    missing = [key for key in scientific_keys if key not in payload]
    if missing:
        raise EpochFingerprintError(f"epoch payload is missing fields: {', '.join(missing)}")
    return {key: payload[key] for key in scientific_keys}


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EpochFingerprintError(f"{field} must be an object")
    return value


def _require_sha256(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise EpochFingerprintError(f"{field} must be a 64-character SHA-256 digest")
    return text


def _validate_table_fingerprint(value: Any, *, field: str) -> None:
    fingerprint = _require_mapping(value, field=field)
    _require_sha256(fingerprint.get("sha256"), field=f"{field}.sha256")
    row_count = fingerprint.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise EpochFingerprintError(f"{field}.row_count must be a non-negative integer")


def _validate_epoch_shape(payload: Mapping[str, Any], *, require_hash: bool) -> None:
    scientific = _epoch_hash_payload(payload)
    if scientific["epoch_schema_version"] != EPOCH_SCHEMA_VERSION:
        raise EpochFingerprintError("unsupported epoch schema version")

    relations = scientific["relevant_relations"]
    if relations != "ALL_NON_SYSTEM_RELATIONS":
        if not isinstance(relations, list) or not relations:
            raise EpochFingerprintError(
                "relevant_relations must be ALL_NON_SYSTEM_RELATIONS or a non-empty list"
            )
        normalized_relations = []
        for position, relation in enumerate(relations):
            relation_map = _require_mapping(
                relation, field=f"relevant_relations[{position}]"
            )
            if set(relation_map) != {"schema", "table"}:
                raise EpochFingerprintError(
                    f"relevant_relations[{position}] must contain exactly schema and table"
                )
            schema = str(relation_map["schema"]).strip()
            table = str(relation_map["table"]).strip()
            if not schema or not table:
                raise EpochFingerprintError(
                    f"relevant_relations[{position}] has an empty schema or table"
                )
            normalized_relations.append((schema, table))
        if normalized_relations != sorted(set(normalized_relations)):
            raise EpochFingerprintError("relevant_relations must be unique and sorted")

    environment = _require_mapping(
        scientific["database_environment"], field="database_environment"
    )
    for field in (
        "postgresql_version",
        "postgresql_server_version_num",
        "hypopg_version",
        "database_name",
        "database_oid",
        "current_user",
        "session_user",
        "search_path",
        "row_security",
    ):
        if not str(environment.get(field, "")).strip():
            raise EpochFingerprintError(f"database_environment.{field} is required")
    collation = _require_mapping(
        environment.get("database_collation"),
        field="database_environment.database_collation",
    )
    for field in ("datcollate", "datctype"):
        if not str(collation.get(field, "")).strip():
            raise EpochFingerprintError(
                f"database_environment.database_collation.{field} is required"
            )
    actual_collation = _require_mapping(
        collation.get("actual_version"),
        field="database_environment.database_collation.actual_version",
    )
    if set(actual_collation) != {"status", "value"}:
        raise EpochFingerprintError("database collation actual_version has an unexpected schema")
    if actual_collation.get("status") == "AVAILABLE":
        if not str(actual_collation.get("value") or "").strip():
            raise EpochFingerprintError("available database collation version is empty")
    elif actual_collation.get("status") in {"UNSUPPORTED", "NOT_AVAILABLE"}:
        if actual_collation.get("value") is not None:
            raise EpochFingerprintError("unavailable database collation version has a value")
    else:
        raise EpochFingerprintError("database collation actual_version has invalid status")
    gucs = _require_mapping(
        environment.get("planner_gucs"), field="database_environment.planner_gucs"
    )
    if set(gucs) != set(PLANNER_GUCS):
        raise EpochFingerprintError(
            "database_environment.planner_gucs must contain exactly the required v0 GUCs"
        )
    for name in PLANNER_GUCS:
        if not str(gucs[name]).strip():
            raise EpochFingerprintError(f"planner GUC {name!r} has an empty value")
    optional_gucs = _require_mapping(
        environment.get("optional_planner_gucs"),
        field="database_environment.optional_planner_gucs",
    )
    if set(optional_gucs) != set(OPTIONAL_PLANNER_GUCS):
        raise EpochFingerprintError(
            "database_environment.optional_planner_gucs must contain exactly the "
            "version-dependent v0 settings"
        )
    for name in OPTIONAL_PLANNER_GUCS:
        record = _require_mapping(
            optional_gucs[name], field=f"optional planner GUC {name!r}"
        )
        if set(record) != {"status", "value"}:
            raise EpochFingerprintError(
                f"optional planner GUC {name!r} has an unexpected schema"
            )
        status = record.get("status")
        value = record.get("value")
        if status == "AVAILABLE":
            if not str(value or "").strip():
                raise EpochFingerprintError(
                    f"optional planner GUC {name!r} is available but empty"
                )
        elif status == "UNSUPPORTED":
            if value is not None:
                raise EpochFingerprintError(
                    f"unsupported optional planner GUC {name!r} has a value"
                )
        else:
            raise EpochFingerprintError(
                f"optional planner GUC {name!r} has invalid status {status!r}"
            )
    enable_gucs = _require_mapping(
        environment.get("enable_planner_gucs"),
        field="database_environment.enable_planner_gucs",
    )
    if not enable_gucs:
        raise EpochFingerprintError("no enable_* planner settings were captured")
    if list(enable_gucs) != sorted(enable_gucs):
        raise EpochFingerprintError("enable_* planner settings must be ordered by name")
    for name, value in enable_gucs.items():
        if not str(name).startswith("enable_") or not str(value).strip():
            raise EpochFingerprintError(f"invalid enable_* planner setting {name!r}")

    statistics = _require_mapping(
        scientific["statistics_fingerprint"], field="statistics_fingerprint"
    )
    _validate_table_fingerprint(
        statistics.get("pg_class_relpages_reltuples"),
        field="statistics_fingerprint.pg_class_relpages_reltuples",
    )
    _validate_table_fingerprint(
        statistics.get("pg_stats"), field="statistics_fingerprint.pg_stats"
    )
    _validate_table_fingerprint(
        statistics.get("pg_statistic_ext"),
        field="statistics_fingerprint.pg_statistic_ext",
    )
    _validate_table_fingerprint(
        statistics.get("pg_statistic_ext_data"),
        field="statistics_fingerprint.pg_statistic_ext_data",
    )
    recorded_statistics_hash = _require_sha256(
        statistics.get("sha256"), field="statistics_fingerprint.sha256"
    )
    statistics_components = dict(statistics)
    statistics_components.pop("sha256", None)
    if recorded_statistics_hash != canonical_sha256(statistics_components):
        raise EpochFingerprintError("statistics_fingerprint aggregate hash mismatch")

    schema = _require_mapping(
        scientific["schema_fingerprint"], field="schema_fingerprint"
    )
    for component in (
        "relation_definitions",
        "columns",
        "constraints",
        "partitions",
        "inheritance",
    ):
        _validate_table_fingerprint(
            schema.get(component), field=f"schema_fingerprint.{component}"
        )
    recorded_schema_hash = _require_sha256(
        schema.get("sha256"), field="schema_fingerprint.sha256"
    )
    schema_components = dict(schema)
    schema_components.pop("sha256", None)
    if recorded_schema_hash != canonical_sha256(schema_components):
        raise EpochFingerprintError("schema_fingerprint aggregate hash mismatch")

    _validate_table_fingerprint(
        scientific["physical_index_fingerprint"], field="physical_index_fingerprint"
    )
    if require_hash:
        _require_sha256(payload.get("epoch_hash"), field="epoch_hash")


def compute_epoch_hash(payload: Mapping[str, Any]) -> str:
    """Compute the canonical epoch hash, ignoring annotations and any old hash."""

    _validate_epoch_shape(payload, require_hash=False)
    return canonical_sha256(_epoch_hash_payload(payload))


def validate_epoch_hash(payload: Mapping[str, Any]) -> str:
    _validate_epoch_shape(payload, require_hash=True)
    expected = str(payload.get("epoch_hash", "")).strip()
    actual = compute_epoch_hash(payload)
    if not expected or expected != actual:
        raise EpochFingerprintError(
            f"epoch hash mismatch: recorded={expected or '<missing>'}, computed={actual}"
        )
    return actual


def _execute_statement(connection: Any, statement: str) -> None:
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(statement)
    except Exception as exc:
        raise EpochFingerprintError(f"epoch transaction setup failed: {exc}") from exc
    finally:
        if cursor is not None:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()


def _rollback_snapshot(connection: Any) -> None:
    rollback = getattr(connection, "rollback", None)
    if not callable(rollback):
        raise EpochFingerprintError(
            "epoch collection requires a DB-API connection with rollback()"
        )
    try:
        rollback()
    except Exception as exc:
        raise EpochFingerprintError(f"cannot close epoch snapshot: {exc}") from exc


def collect_epoch_fingerprint(
    connection: Any,
    relevant_relations: Optional[Iterable[object]] = None,
) -> dict[str, Any]:
    """Capture one optimizer epoch from a consistent snapshot on *connection*.

    The connection is an owned collection session: any prior transaction is
    rolled back, all catalog reads run in one repeatable-read/read-only
    transaction, and the snapshot is rolled back before returning.  A narrowed
    relation scope is explicit; ``None`` captures every non-system relation.
    """

    relevant = _normalize_relevant_relations(relevant_relations)
    _rollback_snapshot(connection)
    _execute_statement(
        connection, "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    try:
        environment_columns = (
            "postgresql_version",
            "server_version_num",
            "database_name",
            "database_oid",
            "current_user",
            "session_user",
            "search_path",
            "row_security",
            "datcollate",
            "datctype",
            "datlocprovider",
            "daticulocale",
            "datcollversion",
        )
        environment_row = _one_row(
            _query_all(
                connection,
                "SELECT version()::text AS postgresql_version, "
                "current_setting('server_version_num')::text AS server_version_num, "
                "current_database()::text AS database_name, d.oid::text AS database_oid, "
                "current_user::text AS current_user, session_user::text AS session_user, "
                "current_setting('search_path')::text AS search_path, "
                "current_setting('row_security')::text AS row_security, "
                "d.datcollate::text AS datcollate, d.datctype::text AS datctype, "
                "(to_jsonb(d)->>'datlocprovider')::text AS datlocprovider, "
                "(to_jsonb(d)->>'daticulocale')::text AS daticulocale, "
                "(to_jsonb(d)->>'datcollversion')::text AS datcollversion "
                "FROM pg_database d WHERE d.datname = current_database()",
            ),
            environment_columns,
            label="database environment",
        )
        environment_values = dict(zip(environment_columns, environment_row))

        collation_function = _one_row(
            _query_all(
                connection,
                "SELECT to_regprocedure('pg_catalog.pg_database_collation_actual_version(oid)')::text",
            ),
            ("function_identity",),
            label="database collation version capability",
        )[0]
        if collation_function is None:
            actual_collation_version = {"status": "UNSUPPORTED", "value": None}
        else:
            actual_value = _one_row(
                _query_all(
                    connection,
                    "SELECT pg_catalog.pg_database_collation_actual_version(oid)::text "
                    "FROM pg_database WHERE datname=current_database()",
                ),
                ("actual_collation_version",),
                label="database collation actual version",
            )[0]
            actual_collation_version = (
                {"status": "AVAILABLE", "value": str(actual_value)}
                if str(actual_value or "").strip()
                else {"status": "NOT_AVAILABLE", "value": None}
            )

        hypopg_row = _one_row(
            _query_all(
                connection,
                "SELECT extversion::text AS hypopg_version "
                "FROM pg_extension WHERE extname = 'hypopg'",
            ),
            ("hypopg_version",),
            label="HypoPG extension",
        )
        if not str(hypopg_row[0]).strip():
            raise EpochFingerprintError("HypoPG extension version is empty")

        requested_gucs = tuple(sorted(set(PLANNER_GUCS + OPTIONAL_PLANNER_GUCS)))
        guc_literal = ", ".join("'" + name + "'" for name in requested_gucs)
        guc_rows = _query_all(
            connection,
            "SELECT name::text AS name, current_setting(name)::text AS setting "
            f"FROM pg_settings WHERE name IN ({guc_literal}) ORDER BY name",
        )
        captured_gucs: dict[str, str] = {}
        for raw_row in guc_rows:
            name, setting = _values(raw_row, ("name", "setting"))
            name_text = str(name)
            if name_text in captured_gucs:
                raise EpochFingerprintError(f"duplicate planner GUC row: {name_text}")
            captured_gucs[name_text] = str(setting)
        missing_gucs = sorted(set(PLANNER_GUCS) - set(captured_gucs))
        unexpected_gucs = sorted(set(captured_gucs) - set(requested_gucs))
        if missing_gucs or unexpected_gucs:
            raise EpochFingerprintError(
                "planner GUC capture is incomplete: "
                f"missing={missing_gucs}, unexpected={unexpected_gucs}"
            )
        gucs = {name: captured_gucs[name] for name in PLANNER_GUCS}
        optional_gucs = {
            name: (
                {"status": "AVAILABLE", "value": captured_gucs[name]}
                if name in captured_gucs
                else {"status": "UNSUPPORTED", "value": None}
            )
            for name in OPTIONAL_PLANNER_GUCS
        }
        enable_rows = _query_all(
            connection,
            "SELECT name::text AS name, current_setting(name)::text AS setting "
            "FROM pg_settings WHERE name LIKE 'enable\\_%' ESCAPE '\\' ORDER BY name",
        )
        enable_gucs = {
            str(name): str(setting)
            for name, setting in (
                _values(row, ("name", "setting")) for row in enable_rows
            )
        }
        if len(enable_gucs) != len(enable_rows) or not enable_gucs:
            raise EpochFingerprintError(
                "enable_* planner GUC capture is empty or contains duplicates"
            )
        enable_gucs = {name: enable_gucs[name] for name in sorted(enable_gucs)}

        pg_class_columns = (
            "schemaname",
            "relname",
            "relkind",
            "relpages",
            "reltuples",
            "relallvisible",
            "reloptions",
        )
        pg_class_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT n.nspname::text AS schemaname, c.relname::text AS relname, "
                "c.relkind::text AS relkind, c.relpages::text AS relpages, "
                "c.reltuples::text AS reltuples, c.relallvisible::text AS relallvisible, "
                "c.reloptions::text AS reloptions "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
                "AND c.relkind IN ('r','p','m','f','v') "
                "ORDER BY n.nspname, c.relname, c.oid",
            ),
            pg_class_columns,
            relevant,
        )
        if relevant is not None:
            observed = {(str(row[0]), str(row[1])) for row in pg_class_rows}
            missing_relations = sorted(set(relevant) - observed)
            if missing_relations:
                raise EpochFingerprintError(
                    "requested relevant relations were not found in pg_class: "
                    + ", ".join(
                        f"{schema}.{table}" for schema, table in missing_relations
                    )
                )

        pg_stats_columns = (
            "schemaname", "tablename", "attname", "inherited", "null_frac",
            "avg_width", "n_distinct", "most_common_vals", "most_common_freqs",
            "histogram_bounds", "correlation", "most_common_elems",
            "most_common_elem_freqs", "elem_count_histogram",
        )
        pg_stats_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT schemaname::text, tablename::text, attname::text, inherited::text, "
                "null_frac::text, avg_width::text, n_distinct::text, "
                "most_common_vals::text, most_common_freqs::text, histogram_bounds::text, "
                "correlation::text, most_common_elems::text, "
                "most_common_elem_freqs::text, elem_count_histogram::text "
                "FROM pg_stats WHERE schemaname <> 'information_schema' "
                "AND schemaname NOT LIKE 'pg_%' "
                "ORDER BY schemaname, tablename, attname, inherited",
            ),
            pg_stats_columns,
            relevant,
        )

        column_columns = (
            "schemaname", "tablename", "attnum", "attname", "type_oid",
            "formatted_type", "atttypmod", "collation_oid", "collation_name",
            "attnotnull", "attisdropped", "attgenerated", "attidentity",
        )
        column_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT n.nspname::text, c.relname::text, a.attnum::text, a.attname::text, "
                "a.atttypid::text, format_type(a.atttypid,a.atttypmod)::text, "
                "a.atttypmod::text, a.attcollation::text, "
                "COALESCE(coll.collname,'')::text, a.attnotnull::text, a.attisdropped::text, "
                "a.attgenerated::text, a.attidentity::text "
                "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "LEFT JOIN pg_collation coll ON coll.oid=a.attcollation "
                "WHERE a.attnum>0 AND n.nspname <> 'information_schema' "
                "AND n.nspname NOT LIKE 'pg_%' AND c.relkind IN ('r','p','m','f') "
                "ORDER BY n.nspname,c.relname,a.attnum",
            ),
            column_columns,
            relevant,
        )

        relation_definition_columns = (
            "schemaname", "relname", "relkind", "view_definition",
            "foreign_server", "foreign_server_options", "foreign_options",
            "foreign_data_wrapper", "foreign_data_wrapper_options",
        )
        relation_definition_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT n.nspname::text, c.relname::text, c.relkind::text, "
                "CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid,true) ELSE NULL END::text, "
                "fs.srvname::text, fs.srvoptions::text, ft.ftoptions::text, "
                "fdw.fdwname::text, fdw.fdwoptions::text FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "LEFT JOIN pg_foreign_table ft ON ft.ftrelid=c.oid "
                "LEFT JOIN pg_foreign_server fs ON fs.oid=ft.ftserver "
                "LEFT JOIN pg_foreign_data_wrapper fdw ON fdw.oid=fs.srvfdw "
                "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
                "AND c.relkind IN ('r','p','m','f','v') "
                "ORDER BY n.nspname,c.relname,c.oid",
            ),
            relation_definition_columns,
            relevant,
        )

        constraint_columns = (
            "schemaname", "tablename", "constraint_name", "constraint_type",
            "validated", "no_inherit", "deferrable", "initially_deferred",
            "definition",
        )
        constraint_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT n.nspname::text, c.relname::text, con.conname::text, "
                "con.contype::text, con.convalidated::text, con.connoinherit::text, "
                "con.condeferrable::text, con.condeferred::text, "
                "pg_get_constraintdef(con.oid,true)::text "
                "FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
                "ORDER BY n.nspname,c.relname,con.conname,con.oid",
            ),
            constraint_columns,
            relevant,
        )

        partition_columns = (
            "schemaname", "tablename", "partition_key", "partition_bound",
        )
        partition_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT n.nspname::text, c.relname::text, "
                "pg_get_partkeydef(c.oid)::text, pg_get_expr(c.relpartbound,c.oid,true)::text "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
                "AND (c.relkind='p' OR c.relispartition) "
                "ORDER BY n.nspname,c.relname,c.oid",
            ),
            partition_columns,
            relevant,
        )
        inheritance_columns = (
            "schemaname", "tablename", "parent_schema", "parent_table", "sequence",
        )
        inheritance_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT cn.nspname::text, child.relname::text, pn.nspname::text, "
                "parent.relname::text, inh.inhseqno::text "
                "FROM pg_inherits inh JOIN pg_class child ON child.oid=inh.inhrelid "
                "JOIN pg_namespace cn ON cn.oid=child.relnamespace "
                "JOIN pg_class parent ON parent.oid=inh.inhparent "
                "JOIN pg_namespace pn ON pn.oid=parent.relnamespace "
                "WHERE cn.nspname <> 'information_schema' AND cn.nspname NOT LIKE 'pg_%' "
                "ORDER BY cn.nspname,child.relname,pn.nspname,parent.relname,inh.inhseqno",
            ),
            inheritance_columns,
            relevant,
        )

        ext_columns = (
            "schemaname", "tablename", "statistics_name", "definition", "target",
        )
        ext_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT n.nspname::text, c.relname::text, x.stxname::text, "
                "pg_get_statisticsobjdef(x.oid)::text, "
                "(to_jsonb(x)->>'stxstattarget')::text FROM pg_statistic_ext x "
                "JOIN pg_class c ON c.oid=x.stxrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
                "ORDER BY n.nspname,c.relname,x.stxname,x.oid",
            ),
            ext_columns,
            relevant,
        )
        ext_data_columns = (
            "schemaname", "tablename", "statistics_name", "ndistinct",
            "dependencies", "mcv", "expr_stats",
        )
        # pg_statistic_ext_data is privilege-sensitive.  It is unnecessary
        # when the selected scope has no extended-statistics definitions; when
        # definitions do exist, any read failure propagates and fails closed.
        ext_data_rows = (
            _filter_rows(
                _query_all(
                    connection,
                    "SELECT n.nspname::text, c.relname::text, x.stxname::text, "
                    "d.stxdndistinct::text, d.stxddependencies::text, d.stxdmcv::text, "
                    "(to_jsonb(d)->>'stxdexpr')::text FROM pg_statistic_ext x "
                    "JOIN pg_class c ON c.oid=x.stxrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_statistic_ext_data d ON d.stxoid=x.oid "
                    "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
                    "ORDER BY n.nspname,c.relname,x.stxname,x.oid",
                ),
                ext_data_columns,
                relevant,
            )
            if ext_rows
            else []
        )

        physical_index_columns = (
            "schemaname", "tablename", "indexname", "indisunique", "indisprimary",
            "indisvalid", "indisready", "indislive", "index_relpages",
            "index_reltuples", "index_reloptions", "tablespace", "index_definition",
        )
        physical_index_rows = _filter_rows(
            _query_all(
                connection,
                "SELECT n.nspname::text, t.relname::text, i.relname::text, "
                "x.indisunique::text, x.indisprimary::text, x.indisvalid::text, "
                "x.indisready::text, x.indislive::text, i.relpages::text, "
                "i.reltuples::text, i.reloptions::text, ts.spcname::text, "
                "pg_get_indexdef(x.indexrelid)::text FROM pg_index x "
                "JOIN pg_class i ON i.oid=x.indexrelid "
                "JOIN pg_class t ON t.oid=x.indrelid "
                "JOIN pg_namespace n ON n.oid=t.relnamespace "
                "LEFT JOIN pg_tablespace ts ON ts.oid=i.reltablespace "
                "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
                "ORDER BY n.nspname,t.relname,i.relname,i.oid",
            ),
            physical_index_columns,
            relevant,
        )

        statistics = {
            "pg_class_relpages_reltuples": _table_fingerprint(
                pg_class_columns, pg_class_rows
            ),
            "pg_stats": _table_fingerprint(pg_stats_columns, pg_stats_rows),
            "pg_statistic_ext": _table_fingerprint(ext_columns, ext_rows),
            "pg_statistic_ext_data": _table_fingerprint(
                ext_data_columns, ext_data_rows
            ),
        }
        statistics["sha256"] = canonical_sha256(statistics)
        schema = {
            "relation_definitions": _table_fingerprint(
                relation_definition_columns, relation_definition_rows
            ),
            "columns": _table_fingerprint(column_columns, column_rows),
            "constraints": _table_fingerprint(constraint_columns, constraint_rows),
            "partitions": _table_fingerprint(partition_columns, partition_rows),
            "inheritance": _table_fingerprint(inheritance_columns, inheritance_rows),
        }
        schema["sha256"] = canonical_sha256(schema)
        physical_indexes = _table_fingerprint(
            physical_index_columns, physical_index_rows
        )

        payload: dict[str, Any] = {
            "epoch_schema_version": EPOCH_SCHEMA_VERSION,
            "relevant_relations": (
                [{"schema": schema_name, "table": table} for schema_name, table in relevant]
                if relevant is not None
                else "ALL_NON_SYSTEM_RELATIONS"
            ),
            "database_environment": {
                "postgresql_version": str(environment_values["postgresql_version"]),
                "postgresql_server_version_num": str(environment_values["server_version_num"]),
                "hypopg_version": str(hypopg_row[0]),
                "database_name": str(environment_values["database_name"]),
                "database_oid": str(environment_values["database_oid"]),
                "current_user": str(environment_values["current_user"]),
                "session_user": str(environment_values["session_user"]),
                "search_path": str(environment_values["search_path"]),
                "row_security": str(environment_values["row_security"]),
                "database_collation": {
                    "datcollate": str(environment_values["datcollate"]),
                    "datctype": str(environment_values["datctype"]),
                    "datlocprovider": environment_values["datlocprovider"],
                    "daticulocale": environment_values["daticulocale"],
                    "datcollversion": environment_values["datcollversion"],
                    "actual_version": actual_collation_version,
                },
                "planner_gucs": gucs,
                "optional_planner_gucs": optional_gucs,
                "enable_planner_gucs": enable_gucs,
            },
            "statistics_fingerprint": statistics,
            "schema_fingerprint": schema,
            "physical_index_fingerprint": physical_indexes,
        }
        payload["epoch_hash"] = compute_epoch_hash(payload)
        return payload
    finally:
        _rollback_snapshot(connection)


__all__ = [
    "EPOCH_SCHEMA_VERSION",
    "PLANNER_GUCS",
    "EpochFingerprintError",
    "canonical_json",
    "canonical_sha256",
    "sha256_file",
    "compute_epoch_hash",
    "validate_epoch_hash",
    "collect_epoch_fingerprint",
]
