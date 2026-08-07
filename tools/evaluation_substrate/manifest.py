"""Manifest construction for Evaluation Substrate v0.

The manifest binds immutable input bytes, the Git worktree state, and a
validated database epoch.  This module contains no AdaSelectPP online imports
and performs no database work itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .epoch_fingerprint import canonical_sha256, sha256_file, validate_epoch_hash
from .schema import (
    CandidateSnapshot,
    CandidateTier,
    ConfigurationSpec,
    QuerySpec,
    load_tier1_candidate_snapshot,
    load_tier2_candidate_snapshot,
)


MANIFEST_SCHEMA_VERSION = "evaluation_substrate_manifest_v0.1"
DEFAULT_OUTPUT_RELATIVE = Path("runs") / "evaluation_substrate_v0"
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ManifestError(RuntimeError):
    """Raised when a complete manifest cannot be constructed or persisted."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_git(repo_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(repo_root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise ManifestError(f"cannot execute Git: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ManifestError(
            f"Git {' '.join(arguments)} failed with exit code {result.returncode}: {detail}"
        )
    return result.stdout.strip()


def collect_git_state(repo_root: Path | str) -> dict[str, Any]:
    """Return the exact commit plus a porcelain dirty-state snapshot."""

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ManifestError(f"repository root is not a directory: {root}")
    commit = _run_git(root, "rev-parse", "--verify", "HEAD")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise ManifestError(f"Git returned an invalid commit id: {commit!r}")
    porcelain = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    entries = porcelain.splitlines() if porcelain else []
    return {
        "git_commit": commit.lower(),
        "git_dirty": bool(entries),
        "git_dirty_status": "DIRTY" if entries else "CLEAN",
        "git_status_porcelain": entries,
        "git_state_source": "GIT_VERIFIED",
    }


def collect_substrate_source_state() -> dict[str, Any]:
    """Hash the exact offline core source bytes, including untracked files."""

    package = Path(__file__).resolve().parent
    files = [
        {"path": path.name, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
        for path in sorted(package.glob("*.py"), key=lambda item: item.name)
    ]
    if not files:
        raise ManifestError("Evaluation Substrate source package is empty")
    return {"files": files, "sha256": canonical_sha256(files)}


def _display_path(path: Path, repo_root: Optional[Path]) -> str:
    resolved = path.resolve()
    if repo_root is not None:
        try:
            return resolved.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            pass
    return str(resolved)


def artifact_record(
    path: Path | str,
    *,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    """Describe and hash one immutable input artifact."""

    artifact = Path(path)
    if not artifact.is_file():
        raise ManifestError(f"input artifact is not a regular file: {artifact}")
    resolved = artifact.resolve()
    before = resolved.stat()
    digest = sha256_file(resolved)
    after = resolved.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise ManifestError(f"input artifact changed while it was being hashed: {resolved}")
    root = Path(repo_root).resolve() if repo_root is not None else None
    return {
        "status": "PRESENT",
        "path": _display_path(resolved, root),
        "sha256": digest,
        "size_bytes": after.st_size,
    }


def _missing_record() -> dict[str, Any]:
    return {
        "status": "NOT_PROVIDED",
        "path": None,
        "sha256": None,
        "size_bytes": None,
    }


def collect_input_artifacts(
    *,
    workload_file: Path | str,
    candidate_snapshot_tier1: Path | str | None = None,
    candidate_snapshot_tier2: Path | str | None = None,
    metrics_file: Path | str | None = None,
    trace_file: Path | str | None = None,
    additional_inputs: Mapping[str, Path | str] | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Hash all declared substrate inputs, with explicit absent optional roles."""

    root = Path(repo_root).resolve() if repo_root is not None else None
    result = {
        "workload": artifact_record(workload_file, repo_root=root),
        "candidate_snapshot_tier1": (
            artifact_record(candidate_snapshot_tier1, repo_root=root)
            if candidate_snapshot_tier1 is not None
            else _missing_record()
        ),
        "candidate_snapshot_tier2": (
            artifact_record(candidate_snapshot_tier2, repo_root=root)
            if candidate_snapshot_tier2 is not None
            else _missing_record()
        ),
        "metrics": (
            artifact_record(metrics_file, repo_root=root)
            if metrics_file is not None
            else _missing_record()
        ),
        "trace": (
            artifact_record(trace_file, repo_root=root)
            if trace_file is not None
            else _missing_record()
        ),
    }
    for raw_role, path in (additional_inputs or {}).items():
        role = str(raw_role).strip()
        if not role or role in result:
            raise ManifestError(f"invalid or duplicate input artifact role: {raw_role!r}")
        result[role] = artifact_record(path, repo_root=root)
    return result


def _validate_run_id(run_id: object) -> str:
    text = str(run_id).strip()
    if not _RUN_ID_PATTERN.fullmatch(text) or text in {".", ".."}:
        raise ManifestError(
            "run_id must be 1-128 characters using only letters, digits, '.', '_', or '-', "
            "must start with a letter or digit, and cannot be '.' or '..'"
        )
    return text


def output_root(repo_root: Path | str | None = None) -> Path:
    root = Path(repo_root).resolve() if repo_root is not None else repository_root()
    destination = (root / DEFAULT_OUTPUT_RELATIVE).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"evaluation output root escapes the repository: {destination}") from exc
    return destination


def create_run_directory(
    run_id: object,
    *,
    repo_root: Path | str | None = None,
    exist_ok: bool = False,
) -> Path:
    """Create ``runs/evaluation_substrate_v0/<run_id>`` without path traversal."""

    safe_id = _validate_run_id(run_id)
    base = output_root(repo_root)
    base.mkdir(parents=True, exist_ok=True)
    destination = (base / safe_id).resolve()
    try:
        destination.relative_to(base)
    except ValueError as exc:  # defensive in case platform path semantics change
        raise ManifestError(f"run directory escapes the evaluation output root: {destination}") from exc
    destination.mkdir(parents=False, exist_ok=exist_ok)
    if not destination.is_dir():
        raise ManifestError(f"run path is not a directory: {destination}")
    return destination


def _utc_timestamp(value: Optional[datetime]) -> str:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ManifestError("created_at must be timezone-aware")
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _copy_json(value: Any, *, label: str) -> Any:
    try:
        # A JSON round trip both detaches caller-owned objects and rejects values
        # that cannot be represented in the persisted manifest.
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{label} is not valid JSON data: {exc}") from exc


def build_tier1_inventory(
    queries: list[QuerySpec] | tuple[QuerySpec, ...],
    configurations: list[ConfigurationSpec] | tuple[ConfigurationSpec, ...],
) -> dict[str, Any]:
    """Build the canonical manifest-bound Tier-1 request inventory."""

    if not queries or any(not isinstance(query, QuerySpec) for query in queries):
        raise ManifestError("Tier-1 inventory requires a non-empty QuerySpec sequence")
    if not configurations or any(
        not isinstance(configuration, ConfigurationSpec)
        for configuration in configurations
    ):
        raise ManifestError(
            "Tier-1 inventory requires a non-empty ConfigurationSpec sequence"
        )
    occurrence_ids = [query.occurrence_id for query in queries]
    if len(occurrence_ids) != len(set(occurrence_ids)):
        raise ManifestError(
            "Tier-1 workload occurrences must have unique occurrence_id values"
        )
    configuration_ids = [
        configuration.configuration_id for configuration in configurations
    ]
    if len(configuration_ids) != len(set(configuration_ids)):
        raise ManifestError("Tier-1 inventory contains a duplicate configuration")
    payload: dict[str, Any] = {
        "inventory_schema_version": "evaluation_substrate_tier1_inventory_v0.1",
        "queries": [
            {
                "occurrence_id": query.occurrence_id,
                "exact_sql_hash": query.exact_sql_hash,
                "template_id": query.template_id,
            }
            for query in queries
        ],
        "configurations": [
            {
                "configuration_id": configuration.configuration_id,
                "canonical_configuration": configuration.canonical_json,
            }
            for configuration in configurations
        ],
    }
    payload["sha256"] = canonical_sha256(payload)
    return payload


def _candidate_tables(snapshot: CandidateSnapshot) -> set[tuple[str, str]]:
    return {("public", row.table) for row in snapshot.rows}


def _validate_relation_scope_for_candidates(
    epoch: Mapping[str, Any], snapshot: CandidateSnapshot
) -> None:
    scope = epoch.get("relevant_relations")
    if scope == "ALL_NON_SYSTEM_RELATIONS":
        return
    if not isinstance(scope, list):
        raise ManifestError("optimizer epoch has an invalid relation scope")
    captured = {
        (str(item.get("schema", "")), str(item.get("table", "")))
        for item in scope
        if isinstance(item, Mapping)
    }
    missing = sorted(_candidate_tables(snapshot) - captured)
    if missing:
        raise ManifestError(
            "epoch relation scope omits candidate tables: "
            + ", ".join(f"{schema}.{table}" for schema, table in missing)
        )


def build_manifest(
    *,
    run_id: object,
    epoch: Mapping[str, Any],
    workload_file: Path | str,
    candidate_snapshot_tier1: Path | str | None = None,
    candidate_snapshot_tier2: Path | str | None = None,
    metrics_file: Path | str | None = None,
    trace_file: Path | str | None = None,
    additional_inputs: Mapping[str, Path | str] | None = None,
    repo_root: Path | str | None = None,
    created_at: datetime | None = None,
    collection_tier: CandidateTier | str,
    tier1_queries: tuple[QuerySpec, ...] | list[QuerySpec] | None = None,
    tier1_configurations: tuple[ConfigurationSpec, ...] | list[ConfigurationSpec] | None = None,
    max_new_optimizer_calls: int | None = None,
    workload_relation_scope_complete: bool = False,
    _test_code_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete manifest without writing it.

    Optional metrics and trace roles remain explicit ``NOT_PROVIDED`` records.
    The selected tier, candidate artifact, relation scope, Tier-1 inventory or
    Tier-2 call guard are mandatory manifest state.
    """

    safe_id = _validate_run_id(run_id)
    root = Path(repo_root).resolve() if repo_root is not None else repository_root()
    tier = CandidateTier.parse(collection_tier)
    selected_path = (
        candidate_snapshot_tier1
        if tier == CandidateTier.TIER1
        else candidate_snapshot_tier2
    )
    if selected_path is None:
        raise ManifestError(f"{tier.value} collection requires its tiered candidate snapshot")
    try:
        selected_snapshot = (
            load_tier1_candidate_snapshot(selected_path)
            if tier == CandidateTier.TIER1
            else load_tier2_candidate_snapshot(selected_path)
        )
    except Exception as exc:
        raise ManifestError(f"invalid candidate snapshot: {exc}") from exc
    try:
        epoch_hash = validate_epoch_hash(epoch)
    except Exception as exc:
        raise ManifestError(f"invalid optimizer epoch: {exc}") from exc
    _validate_relation_scope_for_candidates(epoch, selected_snapshot)
    narrowed_scope = epoch.get("relevant_relations") != "ALL_NON_SYSTEM_RELATIONS"
    if narrowed_scope and not workload_relation_scope_complete:
        raise ManifestError(
            "a narrowed epoch scope requires explicit workload-relation completeness"
        )
    if tier == CandidateTier.TIER2 and narrowed_scope:
        raise ManifestError(
            "Tier-2 lazy collection requires ALL_NON_SYSTEM_RELATIONS because future "
            "request relation coverage cannot be established in v0"
        )

    if tier == CandidateTier.TIER1:
        if max_new_optimizer_calls is not None:
            raise ManifestError("Tier-1 manifest cannot carry a Tier-2 call guard")
        if tier1_queries is None or tier1_configurations is None:
            raise ManifestError("Tier-1 manifest requires its query/configuration inventory")
        tier1_inventory = build_tier1_inventory(
            list(tier1_queries), list(tier1_configurations)
        )
        bound_guard = None
    else:
        if tier1_queries is not None or tier1_configurations is not None:
            raise ManifestError("Tier-2 manifest cannot carry a Tier-1 inventory")
        if (
            isinstance(max_new_optimizer_calls, bool)
            or not isinstance(max_new_optimizer_calls, int)
            or max_new_optimizer_calls < 0
        ):
            raise ManifestError(
                "Tier-2 manifest requires a finite non-negative integer call guard"
            )
        tier1_inventory = None
        bound_guard = max_new_optimizer_calls

    inputs = collect_input_artifacts(
        workload_file=workload_file,
        candidate_snapshot_tier1=candidate_snapshot_tier1,
        candidate_snapshot_tier2=candidate_snapshot_tier2,
        metrics_file=metrics_file,
        trace_file=trace_file,
        additional_inputs=additional_inputs,
        repo_root=root,
    )
    # Production always reads Git directly.  The private injection is retained
    # solely for isolated unit tests which build manifests outside a Git repo.
    captured_code_state = (
        dict(_test_code_state)
        if _test_code_state is not None
        else collect_git_state(root)
    )
    if _test_code_state is not None:
        captured_code_state["git_state_source"] = "TEST_INJECTED"
    captured_code_state["evaluation_substrate_source"] = collect_substrate_source_state()
    required_code_fields = {"git_commit", "git_dirty"}
    if not required_code_fields.issubset(captured_code_state):
        raise ManifestError(
            "code_state is missing required fields: "
            + ", ".join(sorted(required_code_fields - set(captured_code_state)))
        )

    manifest: dict[str, Any] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": safe_id,
        "created_at_utc": _utc_timestamp(created_at),
        "code_state": _copy_json(captured_code_state, label="code_state"),
        "input_artifacts": inputs,
        "epoch_hash": epoch_hash,
        "epoch": _copy_json(epoch, label="epoch"),
        "collection_context": {
            "collection_tier": tier.value,
            "candidate_snapshot_role": f"candidate_snapshot_{tier.value}",
            "candidate_snapshot_sha256": inputs[f"candidate_snapshot_{tier.value}"]["sha256"],
            "workload_sha256": inputs["workload"]["sha256"],
            "relation_scope": _copy_json(epoch["relevant_relations"], label="relation_scope"),
            "workload_relation_scope_status": (
                "EXPLICIT_COMPLETE"
                if narrowed_scope
                else "ALL_NON_SYSTEM_RELATIONS"
            ),
            "tier1_inventory": tier1_inventory,
            "max_new_optimizer_calls": bound_guard,
            "single_writer": True,
        },
    }
    # This checksum excludes itself and is useful when a manifest is copied
    # alongside response tables.  The authoritative epoch remains epoch_hash.
    manifest["manifest_payload_sha256"] = canonical_sha256(manifest)
    validate_manifest(manifest)
    return manifest


def _manifest_target(path_or_directory: Path | str) -> Path:
    path = Path(path_or_directory)
    if path.name == "manifest.json":
        return path
    if path.suffix:
        raise ManifestError("the manifest filename must be exactly 'manifest.json'")
    return path / "manifest.json"


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    required = {
        "manifest_schema_version",
        "run_id",
        "created_at_utc",
        "code_state",
        "input_artifacts",
        "epoch_hash",
        "epoch",
        "collection_context",
        "manifest_payload_sha256",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise ManifestError(f"manifest is missing fields: {', '.join(missing)}")
    unexpected = sorted(set(manifest) - required)
    if unexpected:
        raise ManifestError(f"manifest has unexpected fields: {', '.join(unexpected)}")
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("unsupported manifest schema version")
    _validate_run_id(manifest.get("run_id"))
    timestamp = manifest.get("created_at_utc")
    if not isinstance(timestamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp
    ):
        raise ManifestError("created_at_utc must be a second-precision UTC timestamp")

    code_state = manifest.get("code_state")
    if not isinstance(code_state, Mapping):
        raise ManifestError("manifest code_state must be an object")
    expected_code_fields = {
        "git_commit",
        "git_dirty",
        "git_dirty_status",
        "git_status_porcelain",
        "git_state_source",
        "evaluation_substrate_source",
    }
    if set(code_state) != expected_code_fields:
        raise ManifestError("manifest code_state has an unexpected schema")
    if not re.fullmatch(r"[0-9a-f]{40,64}", str(code_state.get("git_commit", ""))):
        raise ManifestError("manifest code_state.git_commit is invalid")
    dirty = code_state.get("git_dirty")
    porcelain = code_state.get("git_status_porcelain")
    dirty_status = code_state.get("git_dirty_status")
    if not isinstance(dirty, bool):
        raise ManifestError("manifest code_state.git_dirty must be boolean")
    if not isinstance(porcelain, list) or any(
        not isinstance(line, str) or not line for line in porcelain
    ):
        raise ManifestError(
            "manifest code_state.git_status_porcelain must be a list of non-empty strings"
        )
    if dirty != bool(porcelain) or dirty_status != ("DIRTY" if dirty else "CLEAN"):
        raise ManifestError("manifest code_state dirty fields disagree")
    if code_state.get("git_state_source") not in {"GIT_VERIFIED", "TEST_INJECTED"}:
        raise ManifestError("manifest code_state.git_state_source is invalid")
    source_state = code_state.get("evaluation_substrate_source")
    if not isinstance(source_state, Mapping) or set(source_state) != {"files", "sha256"}:
        raise ManifestError("manifest substrate source state has an unexpected schema")
    source_files = source_state.get("files")
    if not isinstance(source_files, list) or not source_files:
        raise ManifestError("manifest substrate source state has no files")
    if source_state.get("sha256") != canonical_sha256(source_files):
        raise ManifestError("manifest substrate source hash mismatch")

    artifacts = manifest.get("input_artifacts")
    if not isinstance(artifacts, Mapping):
        raise ManifestError("manifest input_artifacts must be an object")
    required_artifact_roles = {
        "workload",
        "candidate_snapshot_tier1",
        "candidate_snapshot_tier2",
        "metrics",
        "trace",
    }
    missing_roles = sorted(required_artifact_roles - set(artifacts))
    if missing_roles:
        raise ManifestError(
            "manifest input_artifacts is missing roles: " + ", ".join(missing_roles)
        )
    for role, raw_record in artifacts.items():
        if not isinstance(role, str) or not role:
            raise ManifestError("manifest contains an invalid artifact role")
        if not isinstance(raw_record, Mapping) or set(raw_record) != {
            "status",
            "path",
            "sha256",
            "size_bytes",
        }:
            raise ManifestError(f"manifest artifact {role!r} has an unexpected schema")
        status = raw_record.get("status")
        if status == "PRESENT":
            if not isinstance(raw_record.get("path"), str) or not raw_record["path"]:
                raise ManifestError(f"manifest artifact {role!r} has no path")
            if not re.fullmatch(r"[0-9a-f]{64}", str(raw_record.get("sha256", ""))):
                raise ManifestError(f"manifest artifact {role!r} has an invalid SHA-256")
            size = raw_record.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ManifestError(f"manifest artifact {role!r} has an invalid size")
        elif status == "NOT_PROVIDED":
            if any(
                raw_record.get(field) is not None
                for field in ("path", "sha256", "size_bytes")
            ):
                raise ManifestError(
                    f"manifest artifact {role!r} marks absent values as present"
                )
        else:
            raise ManifestError(f"manifest artifact {role!r} has invalid status")
    if artifacts["workload"].get("status") != "PRESENT":
        raise ManifestError("manifest workload artifact must be present")
    if not any(
        artifacts[role].get("status") == "PRESENT"
        for role in ("candidate_snapshot_tier1", "candidate_snapshot_tier2")
    ):
        raise ManifestError("manifest must contain at least one candidate snapshot")

    epoch = manifest.get("epoch")
    if not isinstance(epoch, Mapping):
        raise ManifestError("manifest epoch must be an object")

    collection = manifest.get("collection_context")
    if not isinstance(collection, Mapping) or set(collection) != {
        "collection_tier",
        "candidate_snapshot_role",
        "candidate_snapshot_sha256",
        "workload_sha256",
        "relation_scope",
        "workload_relation_scope_status",
        "tier1_inventory",
        "max_new_optimizer_calls",
        "single_writer",
    }:
        raise ManifestError("manifest collection_context has an unexpected schema")
    try:
        tier = CandidateTier.parse(collection.get("collection_tier"))
    except Exception as exc:
        raise ManifestError(f"invalid manifest collection tier: {exc}") from exc
    role = f"candidate_snapshot_{tier.value}"
    if collection.get("candidate_snapshot_role") != role:
        raise ManifestError("collection context candidate role disagrees with its tier")
    candidate_record = artifacts.get(role)
    if not isinstance(candidate_record, Mapping) or candidate_record.get("status") != "PRESENT":
        raise ManifestError("manifest-bound candidate snapshot is not present")
    if collection.get("candidate_snapshot_sha256") != candidate_record.get("sha256"):
        raise ManifestError("collection context candidate snapshot hash mismatch")
    if collection.get("workload_sha256") != artifacts["workload"].get("sha256"):
        raise ManifestError("collection context workload hash mismatch")
    if collection.get("single_writer") is not True:
        raise ManifestError("Evaluation Substrate v0 must declare single_writer=true")
    if collection.get("relation_scope") != epoch.get("relevant_relations"):
        raise ManifestError("collection relation scope differs from the epoch scope")
    scope_status = collection.get("workload_relation_scope_status")
    expected_scope_status = (
        "ALL_NON_SYSTEM_RELATIONS"
        if collection.get("relation_scope") == "ALL_NON_SYSTEM_RELATIONS"
        else "EXPLICIT_COMPLETE"
    )
    if scope_status != expected_scope_status:
        raise ManifestError("collection workload relation-scope status is invalid")

    inventory = collection.get("tier1_inventory")
    guard = collection.get("max_new_optimizer_calls")
    if tier == CandidateTier.TIER1:
        if guard is not None or not isinstance(inventory, Mapping):
            raise ManifestError("Tier-1 context requires inventory and no Tier-2 guard")
        if set(inventory) != {
            "inventory_schema_version", "queries", "configurations", "sha256"
        }:
            raise ManifestError("Tier-1 inventory has an unexpected schema")
        recorded_inventory_hash = str(inventory.get("sha256", ""))
        inventory_payload = dict(inventory)
        inventory_payload.pop("sha256", None)
        if recorded_inventory_hash != canonical_sha256(inventory_payload):
            raise ManifestError("Tier-1 inventory hash mismatch")
        if inventory.get("inventory_schema_version") != "evaluation_substrate_tier1_inventory_v0.1":
            raise ManifestError("unsupported Tier-1 inventory schema")
        queries = inventory.get("queries")
        configurations = inventory.get("configurations")
        if not isinstance(queries, list) or not queries:
            raise ManifestError("Tier-1 inventory has no query occurrences")
        if any(
            not isinstance(query, Mapping)
            or set(query)
            != {"occurrence_id", "exact_sql_hash", "template_id"}
            for query in queries
        ):
            raise ManifestError("Tier-1 query occurrence has an unexpected schema")
        occurrence_ids = [str(query["occurrence_id"]) for query in queries]
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise ManifestError("Tier-1 inventory has duplicate occurrence IDs")
        for query in queries:
            if not str(query["occurrence_id"]).strip():
                raise ManifestError("Tier-1 occurrence_id cannot be empty")
            if not re.fullmatch(
                r"[0-9a-f]{64}", str(query["exact_sql_hash"])
            ):
                raise ManifestError("Tier-1 exact_sql_hash is invalid")
            template_id = query["template_id"]
            if template_id is not None and (
                not isinstance(template_id, str) or not template_id.strip()
            ):
                raise ManifestError("Tier-1 template_id must be null or non-empty text")
        if not isinstance(configurations, list) or not configurations:
            raise ManifestError("Tier-1 inventory has no configurations")
    else:
        if inventory is not None:
            raise ManifestError("Tier-2 context cannot contain a Tier-1 inventory")
        if isinstance(guard, bool) or not isinstance(guard, int) or guard < 0:
            raise ManifestError("Tier-2 context requires a non-negative integer guard")

    try:
        epoch_hash = validate_epoch_hash(epoch)
    except Exception as exc:
        raise ManifestError(f"invalid manifest epoch: {exc}") from exc
    if manifest.get("epoch_hash") != epoch_hash:
        raise ManifestError("manifest epoch_hash differs from the embedded epoch")
    without_checksum = dict(manifest)
    recorded_checksum = str(without_checksum.pop("manifest_payload_sha256", ""))
    if not recorded_checksum or recorded_checksum != canonical_sha256(without_checksum):
        raise ManifestError("manifest payload checksum mismatch")


def write_manifest_atomic(
    path_or_directory: Path | str,
    manifest: Mapping[str, Any],
) -> Path:
    """Validate and atomically replace ``manifest.json``."""

    validate_manifest(manifest)
    target = _manifest_target(path_or_directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix="manifest.json.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return target


def initialize_run(
    *,
    run_id: object,
    epoch: Mapping[str, Any],
    workload_file: Path | str,
    candidate_snapshot_tier1: Path | str | None = None,
    candidate_snapshot_tier2: Path | str | None = None,
    metrics_file: Path | str | None = None,
    trace_file: Path | str | None = None,
    additional_inputs: Mapping[str, Path | str] | None = None,
    repo_root: Path | str | None = None,
    created_at: datetime | None = None,
    collection_tier: CandidateTier | str,
    tier1_queries: tuple[QuerySpec, ...] | list[QuerySpec] | None = None,
    tier1_configurations: tuple[ConfigurationSpec, ...] | list[ConfigurationSpec] | None = None,
    max_new_optimizer_calls: int | None = None,
    workload_relation_scope_complete: bool = False,
    _test_code_state: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Create a safe run directory, build its manifest, and persist it."""

    manifest = build_manifest(
        run_id=run_id,
        epoch=epoch,
        workload_file=workload_file,
        candidate_snapshot_tier1=candidate_snapshot_tier1,
        candidate_snapshot_tier2=candidate_snapshot_tier2,
        metrics_file=metrics_file,
        trace_file=trace_file,
        additional_inputs=additional_inputs,
        repo_root=repo_root,
        created_at=created_at,
        collection_tier=collection_tier,
        tier1_queries=tier1_queries,
        tier1_configurations=tier1_configurations,
        max_new_optimizer_calls=max_new_optimizer_calls,
        workload_relation_scope_complete=workload_relation_scope_complete,
        _test_code_state=_test_code_state,
    )
    run_dir = create_run_directory(run_id, repo_root=repo_root, exist_ok=False)
    write_manifest_atomic(run_dir, manifest)
    return run_dir, manifest


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "DEFAULT_OUTPUT_RELATIVE",
    "ManifestError",
    "repository_root",
    "collect_git_state",
    "collect_substrate_source_state",
    "artifact_record",
    "collect_input_artifacts",
    "output_root",
    "create_run_directory",
    "build_tier1_inventory",
    "build_manifest",
    "validate_manifest",
    "write_manifest_atomic",
    "initialize_run",
]
