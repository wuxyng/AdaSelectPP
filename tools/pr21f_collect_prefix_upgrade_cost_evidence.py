#!/usr/bin/env python3
"""PR21f offline cost-evidence gap mapper for prefix-upgrade validation.

This runner consumes PR21e offline validation outputs plus existing replay
artifacts used only to recover pair identifiers. It does not connect to a
database, create/drop indexes, run workload queries, or change AdaSelectPP
runtime behavior.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


OUTPUT_ROOT = Path("runs_pr21f_cost_evidence")
SCRIPT_PATH = Path("tools/pr21f_collect_prefix_upgrade_cost_evidence.py")

STATUS_MEASURED = "MEASURED"
STATUS_ESTIMATED_MODEL = "ESTIMATED_MODEL"
STATUS_NOT_COMPUTABLE = "NOT_COMPUTABLE"

SUB_EXPLICITLY_BOUNDED = "EXPLICITLY_BOUNDED"
SUB_MISSING_STAT = "NOT_COMPUTABLE_MISSING_STAT"
SUB_NO_WRITE_TRACE = "NOT_COMPUTABLE_NO_WRITE_TRACE"
SUB_NO_TRANSITION_TRACE = "NOT_COMPUTABLE_NO_TRANSITION_TRACE"
SUB_READ_ONLY_SCOPE = "READ_ONLY_SCOPED_ESTIMATE"
SUB_PR21E_BLOCKER_PRESERVED = "PR21E_BLOCKER_PRESERVED"

FLOAT_FORMAT_POLICY = ".12g"
STABLE_SORTING_POLICY = "pair_source_status, pair_key, prefix_index, composite_index"
COST_MODEL_NAME = "pr21f_cost_evidence_gap_map"
COST_MODEL_VERSION = "v1"
READ_ONLY_SCOPE_STATEMENT = (
    "this estimate reflects JOB/read-only artifact scope and is not generalizable "
    "to write-heavy workloads"
)

FORBIDDEN_FIELD_FRAGMENTS = (
    "net_benefit",
    "payback",
    "roi",
    "benefit_cost",
    "cost_benefit",
    "accept_score",
    "eligibility_score",
    "worth_it",
)

MODEL_CONSTANTS = {
    "storage_model": {
        "name": "explicit_storage_stats_or_catalog_fields",
        "version": COST_MODEL_VERSION,
        "formula": (
            "MEASURED when catalog-like prefix/composite/delta byte fields are populated; "
            "ESTIMATED_MODEL only when explicit model stats are supplied; otherwise NOT_COMPUTABLE"
        ),
        "required_stats": [
            "prefix_index_size_bytes",
            "composite_index_size_bytes",
            "storage_delta_bytes",
        ],
        "default_fill_policy": "never fill missing stats with defaults",
    },
    "write_model": {
        "name": "write_trace_or_read_only_scope",
        "version": COST_MODEL_VERSION,
        "formula": (
            "MEASURED from explicit write trace rows; READ_ONLY_SCOPED_ESTIMATE is descriptive "
            "only and does not set write cost to zero"
        ),
        "required_stats": ["write_trace_events"],
        "default_fill_policy": "never assume write frequency is zero",
    },
    "transition_model": {
        "name": "transition_trace_only",
        "version": COST_MODEL_VERSION,
        "formula": (
            "MEASURED from explicit transition trace rows; no real index build/drop operations"
        ),
        "required_stats": ["transition_trace_ms"],
        "default_fill_policy": "never create or drop indexes",
    },
}

PR21E_BY_ROUND_COLUMNS = [
    "source_artifact",
    "row_index",
    "round_id",
    "sample_category",
    "gate_threshold",
    "operator_check_status",
    "operator_check_notes",
    "whatif_gain_proxy_field",
    "whatif_gain_proxy_value",
    "primary_status",
    "diagnostic_flags",
    "near_margin_windows",
    "real_evidence_label_field",
    "real_evidence_label",
    "oracle_metadata_field",
    "oracle_metadata_value",
    "gate_outcome",
    "query_level_concentration",
    "top_query_delta_share",
    "storage_evidence_status",
    "write_maintenance_evidence_status",
    "transition_cost_evidence_status",
]

PR21E_SUMMARY_COLUMNS = ["section", "metric", "value", "status", "notes"]

PR20C_PAIR_COLUMNS = ["swap_prefix_index", "width2_index"]
PR20D_PAIR_COLUMNS = ["prefix_index", "composite_index"]
PR20E_PAIR_COLUMNS = ["prefix_index", "composite_index"]
PR20F_PAIR_COLUMNS = [
    "prefix_index",
    "composite_index",
    "prefix_index_size_bytes",
    "composite_index_size_bytes",
    "storage_delta_bytes",
    "storage_delta_ratio",
]

COST_STATS_COLUMNS = [
    "pair_key",
    "storage_delta_bytes_estimate",
    "storage_model_name",
    "storage_model_version",
    "storage_model_assumptions",
    "storage_model_parameters",
]

WRITE_TRACE_COLUMNS = ["pair_key", "write_trace_events", "trace_scope"]
TRANSITION_TRACE_COLUMNS = ["pair_key", "transition_trace_ms", "trace_scope"]

PAIR_OUTPUT_COLUMNS = [
    "pair_key",
    "prefix_index",
    "composite_index",
    "pair_in_pr21e",
    "pair_in_pr20e",
    "pair_in_pr20f",
    "pair_source_status",
    "pr21e_row_count",
    "pr21e_source_artifacts",
    "pr21e_storage_status_values",
    "pr21e_write_status_values",
    "pr21e_transition_status_values",
    "pr21e_blocker_preserved",
    "storage_delta_bytes",
    "storage_delta_bytes_source",
    "storage_delta_bytes_status",
    "storage_delta_bytes_substatus",
    "storage_delta_bytes_model",
    "storage_delta_bytes_model_version",
    "storage_delta_bytes_assumptions",
    "storage_delta_bytes_parameters",
    "storage_delta_bytes_missing_stats",
    "write_maintenance_events",
    "write_maintenance_events_source",
    "write_maintenance_events_status",
    "write_maintenance_events_substatus",
    "write_maintenance_events_model",
    "write_maintenance_events_model_version",
    "write_maintenance_events_assumptions",
    "write_maintenance_events_parameters",
    "write_maintenance_events_missing_stats",
    "transition_cost_ms",
    "transition_cost_ms_source",
    "transition_cost_ms_status",
    "transition_cost_ms_substatus",
    "transition_cost_ms_model",
    "transition_cost_ms_model_version",
    "transition_cost_ms_assumptions",
    "transition_cost_ms_parameters",
    "transition_cost_ms_missing_stats",
]

SUMMARY_COLUMNS = ["section", "metric", "value", "status", "notes"]


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    path: Path
    expected_columns: Tuple[str, ...] = ()
    required_stat_inputs: Tuple[str, ...] = ()
    csv_file: bool = True
    optional: bool = False


@dataclass
class ArtifactAudit:
    spec: ArtifactSpec
    exists: bool
    row_count: int
    content_hash: str
    actual_columns: List[str]
    missing_columns: List[str]
    required_stat_inputs: List[str]
    missing_stat_inputs: List[str]
    rows: List[Dict[str, str]]


@dataclass(frozen=True)
class PairRef:
    prefix_index: str
    composite_index: str

    @property
    def key(self) -> str:
        if not self.prefix_index or not self.composite_index:
            return ""
        return f"{self.prefix_index} -> {self.composite_index}"


@dataclass
class PairAccumulator:
    pair: PairRef
    canonical_rows: List[Mapping[str, str]]
    source_artifacts: Set[str]
    storage_status_values: Set[str]
    write_status_values: Set[str]
    transition_status_values: Set[str]


@dataclass(frozen=True)
class CostEvidence:
    value: str
    source: str
    status: str
    substatus: str
    model: str
    model_version: str
    assumptions: str
    parameters: str
    missing_stats: str


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


def stable_generation_timestamp() -> str:
    commit_time = git_output(["show", "-s", "--format=%cI", "HEAD"])
    if commit_time:
        return commit_time
    return datetime.fromtimestamp(0, tz=timezone.utc).isoformat()


def script_git_version(script_path: Path) -> str:
    version = git_output(["log", "-1", "--format=%H", "--", str(script_path)])
    if version:
        return version
    if script_path.exists():
        return f"WORKTREE_CONTENT_SHA256:{sha256_file(script_path)}"
    return "UNKNOWN"


def fmt_float(value: object) -> str:
    parsed = parse_float(value)
    if parsed is None:
        return ""
    return format(parsed, FLOAT_FORMAT_POLICY)


def parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_csv_rows(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [
            {key: (value if value is not None else "") for key, value in row.items()}
            for row in reader
        ]
        return list(reader.fieldnames or []), rows


def audit_artifact(spec: ArtifactSpec) -> ArtifactAudit:
    if not spec.path.exists():
        return ArtifactAudit(
            spec=spec,
            exists=False,
            row_count=0,
            content_hash=STATUS_NOT_COMPUTABLE,
            actual_columns=[],
            missing_columns=list(spec.expected_columns),
            required_stat_inputs=list(spec.required_stat_inputs),
            missing_stat_inputs=list(spec.required_stat_inputs),
            rows=[],
        )

    if spec.csv_file:
        actual_columns, rows = read_csv_rows(spec.path)
        missing_columns = [col for col in spec.expected_columns if col not in actual_columns]
        missing_stat_inputs = []
        for col in spec.required_stat_inputs:
            if col not in actual_columns or not any(str(row.get(col, "")).strip() for row in rows):
                missing_stat_inputs.append(col)
        row_count = len(rows)
    else:
        text = spec.path.read_text(encoding="utf-8")
        actual_columns = []
        rows = []
        missing_columns = []
        missing_stat_inputs = list(spec.required_stat_inputs)
        row_count = len(text.splitlines())

    return ArtifactAudit(
        spec=spec,
        exists=True,
        row_count=row_count,
        content_hash=sha256_file(spec.path),
        actual_columns=actual_columns,
        missing_columns=missing_columns,
        required_stat_inputs=list(spec.required_stat_inputs),
        missing_stat_inputs=missing_stat_inputs,
        rows=rows,
    )


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def parse_int(value: object) -> Optional[int]:
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def pair_from_source_row(source_artifact: str, row: Mapping[str, str]) -> PairRef:
    if source_artifact == "pr20c_candidates":
        return PairRef(
            prefix_index=str(row.get("swap_prefix_index", "")).strip(),
            composite_index=str(row.get("width2_index", "")).strip(),
        )
    if source_artifact in {"pr20d_rounds", "pr20e_rounds", "pr20f_rounds"}:
        return PairRef(
            prefix_index=str(row.get("prefix_index", "")).strip(),
            composite_index=str(row.get("composite_index", "")).strip(),
        )
    return PairRef("", "")


def source_artifact_audit_name(source_artifact: str) -> str:
    return {
        "pr20c_candidates": "pr20c_candidates",
        "pr20d_rounds": "pr20d_rounds",
        "pr20e_rounds": "pr20e_rounds",
        "pr20f_rounds": "pr20f_rounds",
    }.get(source_artifact, "")


def add_status_values(target: Set[str], value: str) -> None:
    text = str(value).strip()
    if text:
        target.add(text)


def build_canonical_pair_accumulators(audits: Mapping[str, ArtifactAudit]) -> Tuple[Dict[str, PairAccumulator], int]:
    by_round = audits["pr21e_by_round"]
    accumulators: Dict[str, PairAccumulator] = {}
    missing_pair_rows = 0

    for pr21e_row in by_round.rows:
        source = str(pr21e_row.get("source_artifact", "")).strip()
        row_index = parse_int(pr21e_row.get("row_index", ""))
        audit_name = source_artifact_audit_name(source)
        pair = PairRef("", "")
        if audit_name and row_index is not None:
            source_rows = audits.get(audit_name, ArtifactAudit(
                ArtifactSpec(audit_name, Path("")),
                False,
                0,
                STATUS_NOT_COMPUTABLE,
                [],
                [],
                [],
                [],
                [],
            )).rows
            if 0 <= row_index < len(source_rows):
                pair = pair_from_source_row(source, source_rows[row_index])

        pair_key = pair.key
        if not pair_key:
            missing_pair_rows += 1
            pair_key = f"NOT_COMPUTABLE_PAIR:{source}:{pr21e_row.get('row_index', '')}"
            pair = PairRef("", "")

        if pair_key not in accumulators:
            accumulators[pair_key] = PairAccumulator(
                pair=pair,
                canonical_rows=[],
                source_artifacts=set(),
                storage_status_values=set(),
                write_status_values=set(),
                transition_status_values=set(),
            )
        acc = accumulators[pair_key]
        acc.canonical_rows.append(pr21e_row)
        acc.source_artifacts.add(source)
        add_status_values(acc.storage_status_values, pr21e_row.get("storage_evidence_status", ""))
        add_status_values(acc.write_status_values, pr21e_row.get("write_maintenance_evidence_status", ""))
        add_status_values(acc.transition_status_values, pr21e_row.get("transition_cost_evidence_status", ""))

    return accumulators, missing_pair_rows


def pair_set_from_rows(rows: Sequence[Mapping[str, str]], source_artifact: str) -> Set[str]:
    keys = set()
    for row in rows:
        pair = pair_from_source_row(source_artifact, row)
        if pair.key:
            keys.add(pair.key)
    return keys


def first_non_empty(rows: Sequence[Mapping[str, str]], field: str) -> str:
    for row in rows:
        text = str(row.get(field, "")).strip()
        if text:
            return text
    return ""


def matching_rows_by_pair(rows: Sequence[Mapping[str, str]], source_artifact: str) -> Dict[str, List[Mapping[str, str]]]:
    grouped: Dict[str, List[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        key = pair_from_source_row(source_artifact, row).key
        if key:
            grouped[key].append(row)
    return grouped


def preserved_substatus(status_values: Iterable[str]) -> str:
    values = {str(value).strip() for value in status_values if str(value).strip()}
    if any("BLOCKER" in value or "NOT_COMPUTABLE" in value for value in values):
        return SUB_PR21E_BLOCKER_PRESERVED
    return ""


def join_substatuses(*parts: str) -> str:
    values: List[str] = []
    for part in parts:
        for token in str(part).split("|"):
            token = token.strip()
            if token and token not in values:
                values.append(token)
    return "|".join(values)


def storage_evidence(
    pair_key: str,
    pr20f_rows: Sequence[Mapping[str, str]],
    model_stats: Optional[Mapping[str, str]],
    pr21e_storage_status_values: Iterable[str],
) -> CostEvidence:
    preserved = preserved_substatus(pr21e_storage_status_values)
    populated = [
        row for row in pr20f_rows
        if str(row.get("prefix_index_size_bytes", "")).strip()
        and str(row.get("composite_index_size_bytes", "")).strip()
        and str(row.get("storage_delta_bytes", "")).strip()
    ]
    if populated:
        value = fmt_float(populated[0].get("storage_delta_bytes", ""))
        return CostEvidence(
            value=value,
            source=STATUS_MEASURED,
            status=STATUS_MEASURED,
            substatus=join_substatuses(SUB_EXPLICITLY_BOUNDED, preserved),
            model="catalog_size_fields",
            model_version=COST_MODEL_VERSION,
            assumptions="uses populated prefix/composite storage byte fields from replay artifact",
            parameters=json.dumps({"pair_key": pair_key}, sort_keys=True),
            missing_stats="",
        )

    if model_stats is not None and str(model_stats.get("storage_delta_bytes_estimate", "")).strip():
        missing = [
            field for field in (
                "storage_model_name",
                "storage_model_version",
                "storage_model_assumptions",
                "storage_model_parameters",
            )
            if not str(model_stats.get(field, "")).strip()
        ]
        if not missing:
            return CostEvidence(
                value=fmt_float(model_stats.get("storage_delta_bytes_estimate")),
                source=STATUS_ESTIMATED_MODEL,
                status=STATUS_ESTIMATED_MODEL,
                substatus=join_substatuses(SUB_EXPLICITLY_BOUNDED, preserved),
                model=str(model_stats.get("storage_model_name", "")).strip(),
                model_version=str(model_stats.get("storage_model_version", "")).strip(),
                assumptions=str(model_stats.get("storage_model_assumptions", "")).strip(),
                parameters=str(model_stats.get("storage_model_parameters", "")).strip(),
                missing_stats="",
            )

    return CostEvidence(
        value="",
        source=STATUS_NOT_COMPUTABLE,
        status=STATUS_NOT_COMPUTABLE,
        substatus=join_substatuses(SUB_MISSING_STAT, preserved),
        model=MODEL_CONSTANTS["storage_model"]["name"],
        model_version=COST_MODEL_VERSION,
        assumptions="required storage byte stats are missing; no default values were applied",
        parameters=json.dumps(MODEL_CONSTANTS["storage_model"], sort_keys=True),
        missing_stats="prefix_index_size_bytes|composite_index_size_bytes|storage_delta_bytes",
    )


def write_maintenance_evidence(
    pair_key: str,
    write_rows: Sequence[Mapping[str, str]],
    read_only_scope: bool,
    pr21e_write_status_values: Iterable[str],
) -> CostEvidence:
    preserved = preserved_substatus(pr21e_write_status_values)
    populated = [row for row in write_rows if str(row.get("write_trace_events", "")).strip()]
    if populated:
        return CostEvidence(
            value=fmt_float(populated[0].get("write_trace_events")),
            source=STATUS_MEASURED,
            status=STATUS_MEASURED,
            substatus=join_substatuses(SUB_EXPLICITLY_BOUNDED, preserved),
            model="explicit_write_trace",
            model_version=COST_MODEL_VERSION,
            assumptions="uses explicit write trace event count for this pair",
            parameters=json.dumps({"pair_key": pair_key, "trace_scope": populated[0].get("trace_scope", "")}, sort_keys=True),
            missing_stats="",
        )
    if read_only_scope:
        return CostEvidence(
            value="",
            source=STATUS_ESTIMATED_MODEL,
            status=STATUS_ESTIMATED_MODEL,
            substatus=join_substatuses(SUB_READ_ONLY_SCOPE, preserved),
            model=MODEL_CONSTANTS["write_model"]["name"],
            model_version=COST_MODEL_VERSION,
            assumptions=READ_ONLY_SCOPE_STATEMENT,
            parameters=json.dumps({"pair_key": pair_key, "numeric_write_cost_policy": "blank"}, sort_keys=True),
            missing_stats="write_trace_events",
        )
    return CostEvidence(
        value="",
        source=STATUS_NOT_COMPUTABLE,
        status=STATUS_NOT_COMPUTABLE,
        substatus=join_substatuses(SUB_NO_WRITE_TRACE, preserved),
        model=MODEL_CONSTANTS["write_model"]["name"],
        model_version=COST_MODEL_VERSION,
        assumptions="no write trace or DML stats are available",
        parameters=json.dumps(MODEL_CONSTANTS["write_model"], sort_keys=True),
        missing_stats="write_trace_events",
    )


def transition_evidence(
    pair_key: str,
    transition_rows: Sequence[Mapping[str, str]],
    pr21e_transition_status_values: Iterable[str],
) -> CostEvidence:
    preserved = preserved_substatus(pr21e_transition_status_values)
    populated = [row for row in transition_rows if str(row.get("transition_trace_ms", "")).strip()]
    if populated:
        return CostEvidence(
            value=fmt_float(populated[0].get("transition_trace_ms")),
            source=STATUS_MEASURED,
            status=STATUS_MEASURED,
            substatus=join_substatuses(SUB_EXPLICITLY_BOUNDED, preserved),
            model="explicit_transition_trace",
            model_version=COST_MODEL_VERSION,
            assumptions="uses explicit transition trace milliseconds for this pair",
            parameters=json.dumps({"pair_key": pair_key, "trace_scope": populated[0].get("trace_scope", "")}, sort_keys=True),
            missing_stats="",
        )
    return CostEvidence(
        value="",
        source=STATUS_NOT_COMPUTABLE,
        status=STATUS_NOT_COMPUTABLE,
        substatus=join_substatuses(SUB_NO_TRANSITION_TRACE, preserved),
        model=MODEL_CONSTANTS["transition_model"]["name"],
        model_version=COST_MODEL_VERSION,
        assumptions="no transition trace is available; no real index operations were performed",
        parameters=json.dumps(MODEL_CONSTANTS["transition_model"], sort_keys=True),
        missing_stats="transition_trace_ms",
    )


def cost_stats_by_pair(rows: Sequence[Mapping[str, str]]) -> Dict[str, Mapping[str, str]]:
    return {
        str(row.get("pair_key", "")).strip(): row
        for row in rows
        if str(row.get("pair_key", "")).strip()
    }


def trace_rows_by_pair(rows: Sequence[Mapping[str, str]]) -> Dict[str, List[Mapping[str, str]]]:
    grouped: Dict[str, List[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        key = str(row.get("pair_key", "")).strip()
        if key:
            grouped[key].append(row)
    return grouped


def build_pair_rows(
    audits: Mapping[str, ArtifactAudit],
    read_only_scope: bool = False,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    canonical_pairs, missing_pair_rows = build_canonical_pair_accumulators(audits)
    pr20e_pairs = pair_set_from_rows(audits["pr20e_rounds"].rows, "pr20e_rounds")
    pr20f_pairs = pair_set_from_rows(audits["pr20f_rounds"].rows, "pr20f_rounds")
    pr20f_rows_by_pair = matching_rows_by_pair(audits["pr20f_rounds"].rows, "pr20f_rounds")
    stats_by_pair = cost_stats_by_pair(audits["cost_stats"].rows)
    writes_by_pair = trace_rows_by_pair(audits["write_trace"].rows)
    transitions_by_pair = trace_rows_by_pair(audits["transition_trace"].rows)

    rows: List[Dict[str, str]] = []
    for pair_key, acc in canonical_pairs.items():
        in_pr20e = pair_key in pr20e_pairs
        in_pr20f = pair_key in pr20f_pairs
        pair_source_status = (
            STATUS_NOT_COMPUTABLE
            if pair_key.startswith("NOT_COMPUTABLE_PAIR:")
            else STATUS_MEASURED
        )

        storage = storage_evidence(
            pair_key,
            pr20f_rows_by_pair.get(pair_key, []),
            stats_by_pair.get(pair_key),
            acc.storage_status_values,
        )
        write = write_maintenance_evidence(
            pair_key,
            writes_by_pair.get(pair_key, []),
            read_only_scope,
            acc.write_status_values,
        )
        transition = transition_evidence(
            pair_key,
            transitions_by_pair.get(pair_key, []),
            acc.transition_status_values,
        )

        row = {
            "pair_key": pair_key,
            "prefix_index": acc.pair.prefix_index,
            "composite_index": acc.pair.composite_index,
            "pair_in_pr21e": "true",
            "pair_in_pr20e": str(in_pr20e).lower(),
            "pair_in_pr20f": str(in_pr20f).lower(),
            "pair_source_status": pair_source_status,
            "pr21e_row_count": str(len(acc.canonical_rows)),
            "pr21e_source_artifacts": "|".join(sorted(acc.source_artifacts)),
            "pr21e_storage_status_values": "|".join(sorted(acc.storage_status_values)),
            "pr21e_write_status_values": "|".join(sorted(acc.write_status_values)),
            "pr21e_transition_status_values": "|".join(sorted(acc.transition_status_values)),
            "pr21e_blocker_preserved": str(bool(
                preserved_substatus(acc.storage_status_values)
                or preserved_substatus(acc.write_status_values)
                or preserved_substatus(acc.transition_status_values)
            )).lower(),
            "storage_delta_bytes": storage.value,
            "storage_delta_bytes_source": storage.source,
            "storage_delta_bytes_status": storage.status,
            "storage_delta_bytes_substatus": storage.substatus,
            "storage_delta_bytes_model": storage.model,
            "storage_delta_bytes_model_version": storage.model_version,
            "storage_delta_bytes_assumptions": storage.assumptions,
            "storage_delta_bytes_parameters": storage.parameters,
            "storage_delta_bytes_missing_stats": storage.missing_stats,
            "write_maintenance_events": write.value,
            "write_maintenance_events_source": write.source,
            "write_maintenance_events_status": write.status,
            "write_maintenance_events_substatus": write.substatus,
            "write_maintenance_events_model": write.model,
            "write_maintenance_events_model_version": write.model_version,
            "write_maintenance_events_assumptions": write.assumptions,
            "write_maintenance_events_parameters": write.parameters,
            "write_maintenance_events_missing_stats": write.missing_stats,
            "transition_cost_ms": transition.value,
            "transition_cost_ms_source": transition.source,
            "transition_cost_ms_status": transition.status,
            "transition_cost_ms_substatus": transition.substatus,
            "transition_cost_ms_model": transition.model,
            "transition_cost_ms_model_version": transition.model_version,
            "transition_cost_ms_assumptions": transition.assumptions,
            "transition_cost_ms_parameters": transition.parameters,
            "transition_cost_ms_missing_stats": transition.missing_stats,
        }
        rows.append(row)

    rows.sort(key=lambda row: (
        row["pair_source_status"],
        row["pair_key"],
        row["prefix_index"],
        row["composite_index"],
    ))

    canonical_pair_keys = {
        key for key in canonical_pairs
        if not key.startswith("NOT_COMPUTABLE_PAIR:")
    }
    mismatch_counts = {
        "missing_pair_rows": missing_pair_rows,
        "pairs_in_pr21e": len(canonical_pair_keys),
        "pairs_in_pr20e": len(pr20e_pairs),
        "pairs_in_pr20f": len(pr20f_pairs),
        "pairs_in_pr20e_not_pr21e": len(pr20e_pairs - canonical_pair_keys),
        "pairs_in_pr20f_not_pr21e": len(pr20f_pairs - canonical_pair_keys),
        "pairs_in_pr21e_not_pr20e": len(canonical_pair_keys - pr20e_pairs),
        "pairs_in_pr21e_not_pr20f": len(canonical_pair_keys - pr20f_pairs),
    }
    return rows, mismatch_counts


def forbidden_fields_absent(columns: Sequence[str]) -> bool:
    lowered = [col.lower() for col in columns]
    return not any(fragment in col for col in lowered for fragment in FORBIDDEN_FIELD_FRAGMENTS)


def summary_row(section: str, metric: str, value: object, status: str, notes: str) -> Dict[str, str]:
    return {
        "section": section,
        "metric": metric,
        "value": str(value),
        "status": status,
        "notes": notes,
    }


def build_summary_rows(
    audits: Mapping[str, ArtifactAudit],
    pair_rows: Sequence[Mapping[str, str]],
    mismatch_counts: Mapping[str, int],
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for audit in audits.values():
        status = STATUS_MEASURED if audit.exists and not audit.missing_columns else STATUS_NOT_COMPUTABLE
        if audit.missing_stat_inputs:
            status = STATUS_NOT_COMPUTABLE
        notes = f"path={audit.spec.path}"
        if audit.missing_columns:
            notes += f"; missing_columns={'|'.join(audit.missing_columns)}"
        if audit.missing_stat_inputs:
            notes += f"; missing_stat_inputs={'|'.join(audit.missing_stat_inputs)}"
        rows.append(summary_row("schema_stat_audit", audit.spec.name, audit.row_count, status, notes))

    for metric, value in mismatch_counts.items():
        rows.append(summary_row("pair_set", metric, value, STATUS_MEASURED, "reported separately from canonical pair rows"))

    status_counts = Counter(row["storage_delta_bytes_status"] for row in pair_rows)
    for status, count in sorted(status_counts.items()):
        rows.append(summary_row("storage_delta_bytes", status, count, status, "cost-evidence status count"))
    write_counts = Counter(row["write_maintenance_events_status"] for row in pair_rows)
    for status, count in sorted(write_counts.items()):
        rows.append(summary_row("write_maintenance_events", status, count, status, "cost-evidence status count"))
    transition_counts = Counter(row["transition_cost_ms_status"] for row in pair_rows)
    for status, count in sorted(transition_counts.items()):
        rows.append(summary_row("transition_cost_ms", status, count, status, "cost-evidence status count"))

    preserved = sum(1 for row in pair_rows if row.get("pr21e_blocker_preserved") == "true")
    rows.append(summary_row("pr21e_blocker_preservation", "pairs_with_preserved_pr21e_blocker", preserved, STATUS_MEASURED, "PR21e status values are copied into PR21f output rows"))
    rows.append(summary_row("forbidden_calculations", "forbidden_fields_absent", str(forbidden_fields_absent(PAIR_OUTPUT_COLUMNS)).lower(), STATUS_MEASURED, "no net benefit, payback, ROI, ratio, score, or worth label fields"))
    rows.append(summary_row("online_activation", "PR21b-online", "blocked", STATUS_NOT_COMPUTABLE, "PR21b-online remains blocked."))
    return rows


def manifest(
    audits: Mapping[str, ArtifactAudit],
    output_paths: Mapping[str, Path],
) -> Dict[str, object]:
    return {
        "generation_timestamp": stable_generation_timestamp(),
        "current_git_commit": current_git_commit(),
        "script_path": str(SCRIPT_PATH),
        "script_content_hash": sha256_file(SCRIPT_PATH) if SCRIPT_PATH.exists() else STATUS_NOT_COMPUTABLE,
        "script_git_commit_or_version": script_git_version(SCRIPT_PATH),
        "input_files": {
            name: {
                "path": str(audit.spec.path),
                "row_count": audit.row_count,
                "content_hash": audit.content_hash,
                "exists": audit.exists,
            }
            for name, audit in audits.items()
        },
        "cost_model_name": COST_MODEL_NAME,
        "cost_model_version": COST_MODEL_VERSION,
        "cost_model_parameters_constants": MODEL_CONSTANTS,
        "output_files": {name: str(path) for name, path in output_paths.items()},
        "stable_sorting_policy": STABLE_SORTING_POLICY,
        "float_formatting_policy": FLOAT_FORMAT_POLICY,
    }


def report_lines(
    manifest_data: Mapping[str, object],
    audits: Mapping[str, ArtifactAudit],
    pair_rows: Sequence[Mapping[str, str]],
    summary_rows: Sequence[Mapping[str, str]],
    mismatch_counts: Mapping[str, int],
) -> List[str]:
    lines: List[str] = []
    lines.append("# PR21f Offline Cost-Evidence Gap Map")
    lines.append("")
    lines.append("PR21f does not resolve PR21b-online blockers.")
    lines.append("PR21f maps missing cost evidence and bounded estimates.")
    lines.append("PR21b-online remains blocked.")
    lines.append("")
    lines.append("This runner is offline analysis only. It does not change runtime behavior, selector logic, `_choose_config()`, candidate generation, scoring, budgets, materialization, or database state.")
    lines.append("")
    lines.append("## Manifest")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(manifest_data, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Schema And Stat Audit")
    lines.append("")
    lines.append("| artifact | exists | rows | content hash | expected columns | actual columns | missing columns | required stat inputs | missing stat inputs |")
    lines.append("| --- | ---: | ---: | --- | --- | --- | --- | --- | --- |")
    for audit in audits.values():
        lines.append(
            f"| `{audit.spec.name}` | {str(audit.exists).lower()} | {audit.row_count} | "
            f"`{audit.content_hash}` | `{', '.join(audit.spec.expected_columns)}` | "
            f"`{', '.join(audit.actual_columns)}` | `{', '.join(audit.missing_columns)}` | "
            f"`{', '.join(audit.required_stat_inputs)}` | `{', '.join(audit.missing_stat_inputs)}` |"
        )
    lines.append("")
    lines.append("## Pair-Set Map")
    lines.append("")
    lines.append("PR21e by-round rows are the canonical pair-row source. PR20e and PR20f pair sets are reported only as mismatch diagnostics.")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("| --- | ---: |")
    for metric in sorted(mismatch_counts):
        lines.append(f"| `{metric}` | {mismatch_counts[metric]} |")
    lines.append("")
    lines.append("## Cost-Evidence Status Counts")
    lines.append("")
    for section in ("storage_delta_bytes", "write_maintenance_events", "transition_cost_ms"):
        lines.append(f"### {section}")
        lines.append("")
        lines.append("| status | count |")
        lines.append("| --- | ---: |")
        counts = Counter(row[f"{section}_status"] for row in pair_rows)
        for status, count in sorted(counts.items()):
            lines.append(f"| `{status}` | {count} |")
        lines.append("")
    lines.append("## PR21e Blocker Preservation")
    lines.append("")
    preserved = sum(1 for row in pair_rows if row.get("pr21e_blocker_preserved") == "true")
    lines.append(f"PR21e blocker status values are preserved in {preserved} pair rows.")
    lines.append("")
    lines.append("## Forbidden Calculations")
    lines.append("")
    lines.append("No net benefit, payback, ROI, benefit/cost, cost/benefit, score, or worth label fields are produced.")
    lines.append("")
    lines.append("## Summary Rows")
    lines.append("")
    lines.append("| section | metric | value | status | notes |")
    lines.append("| --- | --- | ---: | --- | --- |")
    for row in summary_rows:
        lines.append(f"| `{row['section']}` | `{row['metric']}` | {row['value']} | `{row['status']}` | {row['notes']} |")
    lines.append("")
    lines.append("## Conclusion")
    lines.append("")
    lines.append("PR21f does not resolve PR21b-online blockers.")
    lines.append("PR21f maps missing cost evidence and bounded estimates.")
    lines.append("PR21b-online remains blocked.")
    lines.append("")
    return lines


def artifact_specs(args: argparse.Namespace) -> List[ArtifactSpec]:
    return [
        ArtifactSpec(
            "pr21e_by_round",
            Path(args.pr21e_by_round),
            tuple(PR21E_BY_ROUND_COLUMNS),
            ("storage_evidence_status", "write_maintenance_evidence_status", "transition_cost_evidence_status"),
        ),
        ArtifactSpec("pr21e_summary", Path(args.pr21e_summary), tuple(PR21E_SUMMARY_COLUMNS)),
        ArtifactSpec("pr21e_report", Path(args.pr21e_report), csv_file=False),
        ArtifactSpec("pr20c_candidates", Path(args.pr20c_candidates), tuple(PR20C_PAIR_COLUMNS), optional=True),
        ArtifactSpec("pr20d_rounds", Path(args.pr20d_rounds), tuple(PR20D_PAIR_COLUMNS), optional=True),
        ArtifactSpec("pr20e_rounds", Path(args.pr20e_rounds), tuple(PR20E_PAIR_COLUMNS), optional=True),
        ArtifactSpec(
            "pr20f_rounds",
            Path(args.pr20f_rounds),
            tuple(PR20F_PAIR_COLUMNS),
            ("prefix_index_size_bytes", "composite_index_size_bytes", "storage_delta_bytes"),
            optional=True,
        ),
        ArtifactSpec("cost_stats", Path(args.cost_stats), tuple(COST_STATS_COLUMNS), tuple(COST_STATS_COLUMNS), optional=True),
        ArtifactSpec("write_trace", Path(args.write_trace), tuple(WRITE_TRACE_COLUMNS), ("write_trace_events",), optional=True),
        ArtifactSpec("transition_trace", Path(args.transition_trace), tuple(TRANSITION_TRACE_COLUMNS), ("transition_trace_ms",), optional=True),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr21e-by-round", default="runs_pr21e_offline_validation/pr21e_validation_by_round.csv")
    parser.add_argument("--pr21e-summary", default="runs_pr21e_offline_validation/pr21e_validation_summary.csv")
    parser.add_argument("--pr21e-report", default="runs_pr21e_offline_validation/pr21e_validation_report.md")
    parser.add_argument("--pr20c-candidates", default="runs_pr20c_swap_width2_oracle/pr20c_width2_oracle_candidates.csv")
    parser.add_argument("--pr20d-rounds", default="runs_pr20d_real_exec_prefix_swap/pr20d_real_exec_rounds.csv")
    parser.add_argument("--pr20e-rounds", default="runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_rounds.csv")
    parser.add_argument("--pr20f-rounds", default="runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_rounds.csv")
    parser.add_argument("--cost-stats", default="runs_pr21f_cost_evidence/optional_cost_stats.csv")
    parser.add_argument("--write-trace", default="runs_pr21f_cost_evidence/optional_write_trace.csv")
    parser.add_argument("--transition-trace", default="runs_pr21f_cost_evidence/optional_transition_trace.csv")
    parser.add_argument("--read-only-scope", action="store_true")
    parser.add_argument("--output-dir", default=str(OUTPUT_ROOT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    audits = {spec.name: audit_artifact(spec) for spec in artifact_specs(args)}
    pair_rows, mismatch_counts = build_pair_rows(audits, read_only_scope=args.read_only_scope)
    summary_rows = build_summary_rows(audits, pair_rows, mismatch_counts)

    output_dir = Path(args.output_dir)
    output_paths = {
        "by_pair": output_dir / "pr21f_cost_evidence_by_pair.csv",
        "summary": output_dir / "pr21f_cost_evidence_summary.csv",
        "report": output_dir / "pr21f_cost_evidence_report.md",
    }
    manifest_data = manifest(audits, output_paths)
    report = report_lines(manifest_data, audits, pair_rows, summary_rows, mismatch_counts)

    write_csv(output_paths["by_pair"], PAIR_OUTPUT_COLUMNS, pair_rows)
    write_csv(output_paths["summary"], SUMMARY_COLUMNS, summary_rows)
    output_paths["report"].parent.mkdir(parents=True, exist_ok=True)
    output_paths["report"].write_text("\n".join(report), encoding="utf-8")

    for path in output_paths.values():
        print(f"Wrote {path}")
    print("PR21f does not resolve PR21b-online blockers.")
    print("PR21b-online remains blocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
