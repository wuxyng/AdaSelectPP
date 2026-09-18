"""Strict input schemas for Evaluation Substrate v0.

This module intentionally contains no candidate generation, ranking, selection,
or database access.  It only canonicalizes explicitly supplied scientific
inputs and rejects ambiguous lineage at the boundary.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence, Tuple


CANDIDATE_SNAPSHOT_COLUMNS: Tuple[str, ...] = (
    "candidate_id",
    "table",
    "columns",
    "source",
    "generator_version",
    "snapshot_hash",
)
CONFIGURATION_SERIALIZATION_VERSION = "evaluation-substrate-config-json-v1"
EXECUTED_METRICS_FIELD = "old"
RECOMMENDED_METRICS_FIELD = "new"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class SchemaValidationError(ValueError):
    """Raised when an input cannot be interpreted without guessing."""


class CandidateSnapshotError(SchemaValidationError):
    """Raised when a frozen candidate snapshot violates its schema."""


class MetricsLineageError(SchemaValidationError):
    """Raised when metrics configuration lineage is absent or ambiguous."""


def stable_json(value: object) -> str:
    """Return the one JSON representation used for scientific identifiers."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _required_text(value: object, *, field: str) -> str:
    if value is None:
        raise SchemaValidationError(f"{field} is required")
    text = str(value).strip()
    if not text:
        raise SchemaValidationError(f"{field} is required")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise SchemaValidationError(f"{field} contains a control character")
    return text


def _normalize_identifier(value: object, *, field: str) -> str:
    text = _required_text(value, field=field)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise SchemaValidationError(
            f"{field} must be one unquoted PostgreSQL identifier, got {value!r}"
        )
    # PostgreSQL folds unquoted identifiers to lower case.  Quoted identifiers
    # are deliberately outside the v0 schema, so this normalization is exact.
    return text.lower()


def _normalize_sha256(value: object, *, field: str) -> str:
    text = _required_text(value, field=field)
    if not _SHA256_RE.fullmatch(text):
        raise SchemaValidationError(f"{field} must be a 64-character SHA-256 hex digest")
    return text.lower()


@dataclass(frozen=True, order=True)
class IndexDefinition:
    """One ordered, unqualified hypothetical-index definition."""

    table: str
    columns: Tuple[str, ...]

    def __post_init__(self) -> None:
        table = _normalize_identifier(self.table, field="table")
        if isinstance(self.columns, (str, bytes)):
            raise SchemaValidationError("columns must be an ordered sequence, not a string")
        try:
            raw_columns = tuple(self.columns)
        except TypeError as exc:
            raise SchemaValidationError("columns must be an ordered sequence") from exc
        if not raw_columns:
            raise SchemaValidationError("an index must contain at least one column")
        columns = tuple(
            _normalize_identifier(column, field=f"columns[{position}]")
            for position, column in enumerate(raw_columns)
        )
        if len(set(columns)) != len(columns):
            raise SchemaValidationError("an index cannot contain a duplicate column")
        object.__setattr__(self, "table", table)
        object.__setattr__(self, "columns", columns)

    @property
    def canonical_text(self) -> str:
        """Human-readable canonical form compatible with existing artifacts."""

        return f"{self.table}({','.join(self.columns)})"

    @property
    def canonical_json(self) -> str:
        return stable_json(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {"table": self.table, "columns": list(self.columns)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IndexDefinition":
        if not isinstance(value, Mapping):
            raise SchemaValidationError("index definition must be an object")
        if set(value) != {"table", "columns"}:
            raise SchemaValidationError(
                "index definition must contain exactly 'table' and 'columns'"
            )
        columns = value["columns"]
        if not isinstance(columns, (list, tuple)):
            raise SchemaValidationError("index columns must be a JSON array")
        return cls(table=value["table"], columns=tuple(columns))

    @classmethod
    def from_canonical_text(cls, value: object) -> "IndexDefinition":
        text = _required_text(value, field="index")
        match = re.fullmatch(r"([^(),;]+)\(([^();]*)\)", text)
        if match is None:
            raise SchemaValidationError(f"invalid canonical index definition: {value!r}")
        raw_columns = match.group(2)
        if not raw_columns:
            raise SchemaValidationError("an index must contain at least one column")
        columns = tuple(raw_columns.split(","))
        if any(not column for column in columns):
            raise SchemaValidationError(f"invalid canonical index definition: {value!r}")
        result = cls(table=match.group(1), columns=columns)
        if result.canonical_text != text:
            raise SchemaValidationError(
                f"index definition is not canonical; expected {result.canonical_text!r}"
            )
        return result


@dataclass(frozen=True)
class ConfigurationSpec:
    """An unordered set of indexes with deterministic serialization and ID."""

    indexes: Tuple[IndexDefinition, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.indexes, (str, bytes)):
            raise SchemaValidationError("configuration indexes must be a sequence")
        try:
            indexes = tuple(self.indexes)
        except TypeError as exc:
            raise SchemaValidationError("configuration indexes must be a sequence") from exc
        if any(not isinstance(index, IndexDefinition) for index in indexes):
            raise SchemaValidationError(
                "configuration indexes must all be IndexDefinition instances"
            )
        if len(set(indexes)) != len(indexes):
            raise SchemaValidationError("configuration contains a duplicate index")
        object.__setattr__(self, "indexes", tuple(sorted(indexes)))

    @property
    def canonical_json(self) -> str:
        return stable_json([index.to_dict() for index in self.indexes])

    @property
    def canonical_text(self) -> str:
        return ";".join(index.canonical_text for index in self.indexes)

    @property
    def configuration_id(self) -> str:
        payload = stable_json(
            {
                "configuration": [index.to_dict() for index in self.indexes],
                "serialization_version": CONFIGURATION_SERIALIZATION_VERSION,
            }
        )
        return "cfg_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def serialize(self) -> str:
        return self.canonical_json

    def __iter__(self):
        return iter(self.indexes)

    def __len__(self) -> int:
        return len(self.indexes)

    @classmethod
    def from_canonical_json(cls, value: object) -> "ConfigurationSpec":
        text = _required_text(value, field="configuration JSON")
        try:
            parsed = json.loads(text)
        except Exception as exc:
            raise SchemaValidationError("configuration is not valid JSON") from exc
        if not isinstance(parsed, list):
            raise SchemaValidationError("configuration JSON must be an array")
        return cls(tuple(IndexDefinition.from_mapping(item) for item in parsed))


def configuration_id(indexes: ConfigurationSpec | Iterable[IndexDefinition]) -> str:
    """Return the v0 ID for a configuration or explicit index iterable."""

    configuration = indexes if isinstance(indexes, ConfigurationSpec) else ConfigurationSpec(tuple(indexes))
    return configuration.configuration_id


def serialize_configuration(indexes: ConfigurationSpec | Iterable[IndexDefinition]) -> str:
    configuration = indexes if isinstance(indexes, ConfigurationSpec) else ConfigurationSpec(tuple(indexes))
    return configuration.canonical_json


@dataclass(frozen=True, init=False)
class QuerySpec:
    """One explicitly identified workload occurrence passed to ``reveal``."""

    occurrence_id: str
    sql: str
    template_id: str | None

    def __init__(
        self,
        occurrence_id: object = None,
        sql: object = None,
        template_id: object = None,
        *,
        query_id: object = None,
    ) -> None:
        """Create one occurrence while accepting ``query_id`` as a strict alias.

        ``query_id`` exists only for temporary source compatibility. Supplying
        both names is rejected even when their text matches, so call sites
        cannot silently disagree about which identity they are providing.
        """

        if occurrence_id is not None and query_id is not None:
            raise SchemaValidationError(
                "occurrence_id and compatibility alias query_id cannot both be supplied"
            )
        raw_occurrence = query_id if occurrence_id is None else occurrence_id
        normalized_occurrence = _required_text(
            raw_occurrence, field="occurrence_id"
        )
        if sql is None:
            raise SchemaValidationError("sql is required")
        exact_sql = str(sql)
        if not exact_sql.strip():
            raise SchemaValidationError("sql is required")
        if "\x00" in exact_sql:
            raise SchemaValidationError("sql contains a NUL byte")
        if any(
            ord(character) < 32 and character not in {"\t", "\n", "\r"}
            for character in exact_sql
        ):
            raise SchemaValidationError("sql contains an unsupported control character")
        normalized_template = (
            None
            if template_id is None
            else _required_text(template_id, field="template_id")
        )
        object.__setattr__(self, "occurrence_id", normalized_occurrence)
        object.__setattr__(self, "sql", exact_sql)
        object.__setattr__(self, "template_id", normalized_template)

    @property
    def query_id(self) -> str:
        """Temporary compatibility alias for :attr:`occurrence_id`."""

        return self.occurrence_id

    @property
    def exact_sql_hash(self) -> str:
        """SHA-256 of the exact SQL bytes, including literals and whitespace."""

        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def query_hash(self) -> str:
        """Temporary compatibility alias for :attr:`exact_sql_hash`."""

        return self.exact_sql_hash

    @property
    def query_sha256(self) -> str:
        return self.exact_sql_hash


class CandidateTier(str, Enum):
    TIER1 = "tier1"
    TIER2 = "tier2"

    @classmethod
    def parse(cls, value: object) -> "CandidateTier":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower().replace("-", "").replace("_", "")
        if text == "tier1":
            return cls.TIER1
        if text == "tier2":
            return cls.TIER2
        raise CandidateSnapshotError("candidate tier must be explicitly 'tier1' or 'tier2'")


@dataclass(frozen=True)
class CandidateSnapshotRow:
    candidate_id: str
    table: str
    columns: Tuple[str, ...]
    source: str
    generator_version: str
    snapshot_hash: str

    def __post_init__(self) -> None:
        try:
            candidate_id = _required_text(self.candidate_id, field="candidate_id")
            source = _required_text(self.source, field="source")
            generator_version = _required_text(
                self.generator_version, field="generator_version"
            )
            index = IndexDefinition(self.table, self.columns)
            snapshot_hash = _normalize_sha256(self.snapshot_hash, field="snapshot_hash")
        except SchemaValidationError as exc:
            raise CandidateSnapshotError(str(exc)) from exc
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "table", index.table)
        object.__setattr__(self, "columns", index.columns)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "generator_version", generator_version)
        object.__setattr__(self, "snapshot_hash", snapshot_hash)

    @property
    def index_definition(self) -> IndexDefinition:
        return IndexDefinition(self.table, self.columns)

    @property
    def index(self) -> IndexDefinition:
        return self.index_definition

    def to_csv_row(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "table": self.table,
            "columns": ",".join(self.columns),
            "source": self.source,
            "generator_version": self.generator_version,
            "snapshot_hash": self.snapshot_hash,
        }


@dataclass(frozen=True)
class CandidateSnapshot:
    tier: CandidateTier
    rows: Tuple[CandidateSnapshotRow, ...]

    def __post_init__(self) -> None:
        tier = CandidateTier.parse(self.tier)
        if isinstance(self.rows, (str, bytes)):
            raise CandidateSnapshotError("candidate rows must be a sequence")
        rows = tuple(self.rows)
        if not rows:
            raise CandidateSnapshotError("candidate snapshot is empty")
        if any(not isinstance(row, CandidateSnapshotRow) for row in rows):
            raise CandidateSnapshotError(
                "candidate snapshot rows must be CandidateSnapshotRow instances"
            )
        candidate_ids = [row.candidate_id for row in rows]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise CandidateSnapshotError("candidate snapshot contains duplicate candidate_id values")
        definitions = [row.index_definition for row in rows]
        if len(set(definitions)) != len(definitions):
            raise CandidateSnapshotError(
                "candidate snapshot assigns more than one candidate_id to one index definition"
            )
        snapshot_hashes = {row.snapshot_hash for row in rows}
        if len(snapshot_hashes) != 1:
            raise CandidateSnapshotError("candidate snapshot rows disagree on snapshot_hash")
        object.__setattr__(self, "tier", tier)
        object.__setattr__(self, "rows", rows)

    @property
    def candidates(self) -> Tuple[CandidateSnapshotRow, ...]:
        return self.rows

    @property
    def snapshot_hash(self) -> str:
        return self.rows[0].snapshot_hash

    def by_id(self) -> dict[str, CandidateSnapshotRow]:
        return {row.candidate_id: row for row in self.rows}


def _parse_candidate_columns(value: object) -> Tuple[str, ...]:
    text = _required_text(value, field="columns")
    parts = tuple(part.strip() for part in text.split(","))
    if not parts or any(not part for part in parts):
        raise CandidateSnapshotError(
            "columns must be a non-empty comma-separated ordered identifier list"
        )
    return parts


def load_candidate_snapshot(
    path: Path | str, *, tier: CandidateTier | str
) -> CandidateSnapshot:
    """Load one Tier-1 or Tier-2 snapshot without generating candidates."""

    snapshot_path = Path(path)
    parsed_tier = CandidateTier.parse(tier)
    if not snapshot_path.is_file():
        raise CandidateSnapshotError(f"candidate snapshot is not a file: {snapshot_path}")

    rows = []
    try:
        with snapshot_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            header = tuple(reader.fieldnames or ())
            if header != CANDIDATE_SNAPSHOT_COLUMNS:
                raise CandidateSnapshotError(
                    f"{snapshot_path}: expected exact header {CANDIDATE_SNAPSHOT_COLUMNS!r}, "
                    f"got {header!r}"
                )
            for line_number, row in enumerate(reader, start=2):
                try:
                    rows.append(
                        CandidateSnapshotRow(
                            candidate_id=row["candidate_id"],
                            table=row["table"],
                            columns=_parse_candidate_columns(row["columns"]),
                            source=row["source"],
                            generator_version=row["generator_version"],
                            snapshot_hash=row["snapshot_hash"],
                        )
                    )
                except (KeyError, SchemaValidationError) as exc:
                    raise CandidateSnapshotError(
                        f"{snapshot_path}:{line_number}: {exc}"
                    ) from exc
    except UnicodeError as exc:
        raise CandidateSnapshotError(f"candidate snapshot is not valid UTF-8: {snapshot_path}") from exc
    return CandidateSnapshot(parsed_tier, tuple(rows))


def load_tier1_candidate_snapshot(path: Path | str) -> CandidateSnapshot:
    return load_candidate_snapshot(path, tier=CandidateTier.TIER1)


def load_tier2_candidate_snapshot(path: Path | str) -> CandidateSnapshot:
    return load_candidate_snapshot(path, tier=CandidateTier.TIER2)


def _require_executed_metrics_field(field: object) -> None:
    field_name = str(field or "").strip()
    if field_name == RECOMMENDED_METRICS_FIELD:
        raise MetricsLineageError(
            "metrics field 'new' is a post-window recommendation; only the explicitly "
            "requested executed field 'old' may enter the substrate"
        )
    if field_name != EXECUTED_METRICS_FIELD:
        raise MetricsLineageError(
            "the executed metrics field must be named explicitly as 'old'; no default or "
            "unlabelled configuration is permitted"
        )


def parse_metrics_configuration(
    value_or_row: object, *, field: str
) -> ConfigurationSpec:
    """Parse an executed metrics configuration with explicit ``field='old'``.

    ``value_or_row`` may be either the literal value from the ``old`` cell or a
    row mapping containing that cell.  Passing ``new`` is always an error, and
    omitting ``field`` is a Python call error rather than an implicit default.
    """

    _require_executed_metrics_field(field)
    if isinstance(value_or_row, Mapping):
        if EXECUTED_METRICS_FIELD not in value_or_row:
            raise MetricsLineageError("metrics row is missing executed field 'old'")
        raw_value = value_or_row[EXECUTED_METRICS_FIELD]
    else:
        raw_value = value_or_row

    if raw_value is None:
        raise MetricsLineageError("metrics row has no value for executed field 'old'")
    raw = str(raw_value).strip()
    if not raw:
        raise MetricsLineageError(
            "metrics 'old' configuration is empty; use the explicit literal [] "
            "for an executed empty configuration"
        )
    try:
        parsed = ast.literal_eval(raw)
    except Exception as exc:
        raise MetricsLineageError("metrics 'old' configuration is not a Python literal") from exc
    if not isinstance(parsed, (list, tuple)):
        raise MetricsLineageError("metrics 'old' configuration must be a list or tuple")

    indexes = []
    for position, item in enumerate(parsed):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise MetricsLineageError(
                f"metrics 'old' item {position} must be a (table, columns) pair"
            )
        table, columns = item
        if isinstance(columns, str):
            columns = (columns,)
        if not isinstance(columns, (list, tuple)):
            raise MetricsLineageError(
                f"metrics 'old' item {position} columns must be a list or tuple"
            )
        try:
            indexes.append(IndexDefinition(table, tuple(columns)))
        except SchemaValidationError as exc:
            raise MetricsLineageError(f"invalid metrics 'old' item {position}: {exc}") from exc
    try:
        return ConfigurationSpec(tuple(indexes))
    except SchemaValidationError as exc:
        raise MetricsLineageError(f"invalid metrics 'old' configuration: {exc}") from exc


def configuration_from_metrics_row(
    row: Mapping[str, object], *, field: str
) -> ConfigurationSpec:
    if not isinstance(row, Mapping):
        raise MetricsLineageError("metrics row must be a mapping")
    return parse_metrics_configuration(row, field=field)


def load_executed_configurations(
    metrics_csv: Path | str, *, field: str, round_field: str = "round"
) -> dict[int, ConfigurationSpec]:
    """Load per-round executed configurations; ``field='old'`` is mandatory."""

    _require_executed_metrics_field(field)
    path = Path(metrics_csv)
    if not path.is_file():
        raise MetricsLineageError(f"metrics input is not a file: {path}")
    configurations: dict[int, ConfigurationSpec] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())
        if len(header) != len(set(header)):
            raise MetricsLineageError(f"{path}: duplicate metrics column names")
        required = {round_field, EXECUTED_METRICS_FIELD}
        missing = sorted(required - set(header))
        if missing:
            raise MetricsLineageError(f"{path}: missing required metrics columns {missing}")
        for line_number, row in enumerate(reader, start=2):
            round_text = str(row.get(round_field, "")).strip()
            if round_text.upper() == "SUMMARY":
                continue
            if not round_text:
                raise MetricsLineageError(f"{path}:{line_number}: missing {round_field}")
            try:
                round_id = int(round_text)
            except ValueError as exc:
                raise MetricsLineageError(
                    f"{path}:{line_number}: {round_field} must be an integer"
                ) from exc
            if round_id < 0:
                raise MetricsLineageError(f"{path}:{line_number}: negative {round_field}")
            if round_id in configurations:
                raise MetricsLineageError(f"{path}:{line_number}: duplicate round {round_id}")
            try:
                configurations[round_id] = parse_metrics_configuration(row, field=field)
            except MetricsLineageError as exc:
                raise MetricsLineageError(f"{path}:{line_number}: {exc}") from exc
    if not configurations:
        raise MetricsLineageError(f"no executed configurations found in {path}")
    return configurations


# Deliberately explicit aliases for callers that prefer the longer names.  All
# retain the mandatory ``field`` keyword and therefore cannot default to new.
load_executed_metrics_configurations = load_executed_configurations
parse_executed_metrics_configuration = parse_metrics_configuration


__all__ = [
    "CANDIDATE_SNAPSHOT_COLUMNS",
    "CONFIGURATION_SERIALIZATION_VERSION",
    "EXECUTED_METRICS_FIELD",
    "RECOMMENDED_METRICS_FIELD",
    "CandidateSnapshot",
    "CandidateSnapshotError",
    "CandidateSnapshotRow",
    "CandidateTier",
    "ConfigurationSpec",
    "IndexDefinition",
    "MetricsLineageError",
    "QuerySpec",
    "SchemaValidationError",
    "configuration_from_metrics_row",
    "configuration_id",
    "load_candidate_snapshot",
    "load_executed_configurations",
    "load_executed_metrics_configurations",
    "load_tier1_candidate_snapshot",
    "load_tier2_candidate_snapshot",
    "parse_executed_metrics_configuration",
    "parse_metrics_configuration",
    "serialize_configuration",
    "stable_json",
]
