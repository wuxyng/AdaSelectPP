"""Mandatory provenance and single-writer context for Evaluation Substrate v0.

The context is the only object accepted by the public ``reveal()`` operation.
It binds immutable inputs, candidate membership, the manifest epoch, the
session-local HypoPG backend, determinism authorization, and the response log.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .cost_store import (
    ConflictingStoredResponseError,
    CostStore,
    MissingOptimizerResponseError,
)
from .evidence import EvidenceLedger, ExactResponseKey
from .epoch_fingerprint import canonical_sha256, sha256_file
from .manifest import (
    build_tier1_inventory,
    collect_substrate_source_state,
    validate_manifest,
)
from .reveal import (
    CollectionSummary,
    DeterminismValidationRequired,
    OptimizerNonDeterminismError,
    RevealError,
    RevealResult,
    _HypoPGSession,
    _RevealService,
    _result_from_stored,
)
from .schema import (
    CandidateSnapshot,
    CandidateTier,
    ConfigurationSpec,
    IndexDefinition,
    QuerySpec,
    load_tier1_candidate_snapshot,
    load_tier2_candidate_snapshot,
)


GATE_SCHEMA_VERSION = "evaluation_substrate_determinism_gate_v0.1"
WRITER_LOCK_FILENAME = "optimizer_responses.writer.lock"


class RunContextError(RuntimeError):
    """Raised when run provenance cannot be verified exactly."""


class ArtifactDriftError(RunContextError):
    """Raised when a manifest-bound input no longer has its recorded bytes."""


class WriterLockError(RunContextError):
    """Raised when a response store already has an active or stale writer lock."""


class DeterminismAuthorizationError(RunContextError):
    """Raised when a persisted gate cannot authorize this exact context."""


@dataclass(frozen=True)
class DeterminismAuthorization:
    run_id: str
    manifest_checksum: str
    epoch_hash: str
    occurrence_id: str
    exact_sql_hash: str
    template_id: str | None
    configuration_id: str
    candidate_snapshot_sha256: str
    charged_measurements: int
    optimizer_costs_sha256: str
    canonical_plan_hash: str
    report_sha256: str

    @property
    def query_id(self) -> str:
        """Compatibility alias for ``occurrence_id``."""

        return self.occurrence_id

    @property
    def query_hash(self) -> str:
        """Compatibility alias for ``exact_sql_hash``."""

        return self.exact_sql_hash


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class _WriterLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.token = uuid.uuid4().hex
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "lock_schema_version": "evaluation_substrate_writer_lock_v0",
            "token": self.token,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "instruction": (
                "Do not remove automatically. Inspect the recorded process and run "
                "artifacts, then explicitly remove only after confirming no writer exists."
            ),
        }
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise WriterLockError(
                f"response store writer lock already exists: {self.path}; "
                "treat it as active or unexplained stale state until inspected"
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise WriterLockError(
                f"cannot verify owned writer lock before release: {self.path}"
            ) from exc
        if record.get("token") != self.token:
            raise WriterLockError(
                f"writer lock ownership changed unexpectedly: {self.path}"
            )
        self.path.unlink()
        self._held = False


def inspect_writer_lock(run_directory: Path | str) -> Optional[dict[str, Any]]:
    """Return lock metadata for explicit operator inspection; never clears it."""

    path = Path(run_directory) / WRITER_LOCK_FILENAME
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WriterLockError(f"writer lock is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise WriterLockError(f"writer lock has invalid content: {path}")
    return value


def _load_manifest(run_directory: Path) -> dict[str, Any]:
    path = run_directory / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RunContextError(f"cannot read run manifest: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunContextError("run manifest must contain a JSON object")
    validate_manifest(value)
    return value


def _artifact_path(record: Mapping[str, Any], repo_root: Path) -> Path:
    raw = record.get("path")
    if not isinstance(raw, str) or not raw:
        raise RunContextError("manifest artifact has no path")
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _verify_artifacts(manifest: Mapping[str, Any], repo_root: Path) -> None:
    artifacts = manifest["input_artifacts"]
    for role, record in artifacts.items():
        if record.get("status") != "PRESENT":
            continue
        path = _artifact_path(record, repo_root)
        try:
            current_hash = sha256_file(path)
            current_size = path.stat().st_size
        except Exception as exc:
            raise ArtifactDriftError(
                f"cannot revalidate manifest artifact {role!r}: {path}: {exc}"
            ) from exc
        if current_hash != record.get("sha256") or current_size != record.get("size_bytes"):
            raise ArtifactDriftError(
                f"manifest artifact drift detected for {role!r}: {path}"
            )


def _verify_substrate_source(manifest: Mapping[str, Any]) -> None:
    expected = manifest["code_state"]["evaluation_substrate_source"]
    if collect_substrate_source_state() != expected:
        raise ArtifactDriftError(
            "Evaluation Substrate source bytes differ from the manifest-bound code"
        )


def _scope_argument(scope: object) -> Optional[Tuple[Tuple[str, str], ...]]:
    if scope == "ALL_NON_SYSTEM_RELATIONS":
        return None
    if not isinstance(scope, list) or not scope:
        raise RunContextError("manifest relation scope is invalid")
    return tuple(
        (str(item["schema"]), str(item["table"]))
        for item in scope
        if isinstance(item, Mapping)
    )


class EvaluationRunContext:
    """Validated mandatory context accepted by the public ``reveal()`` API."""

    __slots__ = (
        "_run_directory",
        "_repo_root",
        "_manifest",
        "_run_id",
        "_collection_tier",
        "_candidate_snapshot",
        "_epoch_hash",
        "_service",
        "_writer_lock",
        "_collection_enabled",
        "_closed",
        "_context_lock",
        "_allowed_indexes",
        "_evidence_ledger",
    )

    def __init__(
        self,
        *,
        run_directory: Path,
        repo_root: Path,
        manifest: dict[str, Any],
        candidate_snapshot: CandidateSnapshot,
        service: _RevealService,
        writer_lock: _WriterLock,
        collection_enabled: bool,
        evidence_ledger: EvidenceLedger | None,
    ) -> None:
        self._run_directory = run_directory
        self._repo_root = repo_root
        self._manifest = json.loads(json.dumps(manifest))
        self._run_id = str(manifest["run_id"])
        self._collection_tier = CandidateTier.parse(
            manifest["collection_context"]["collection_tier"]
        )
        self._candidate_snapshot = candidate_snapshot
        self._epoch_hash = str(manifest["epoch_hash"])
        self._service = service
        self._writer_lock = writer_lock
        self._collection_enabled = collection_enabled
        self._closed = False
        self._context_lock = threading.RLock()
        self._allowed_indexes = {
            row.index_definition for row in candidate_snapshot.rows
        }
        self._evidence_ledger = evidence_ledger

    @property
    def run_directory(self) -> Path:
        return self._run_directory

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a detached copy; caller mutation cannot alter run bindings."""

        return json.loads(json.dumps(self._manifest))

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def collection_tier(self) -> CandidateTier:
        return self._collection_tier

    @property
    def candidate_snapshot(self) -> CandidateSnapshot:
        return self._candidate_snapshot

    @property
    def epoch_hash(self) -> str:
        return self._epoch_hash

    @property
    def physical_optimizer_calls(self) -> int:
        return self._service.physical_optimizer_calls

    @property
    def charged_policy_probes(self) -> int:
        if self._evidence_ledger is None:
            return 0
        return self._evidence_ledger.charged_policy_probes

    @property
    def evidence_session_id(self) -> str | None:
        if self._evidence_ledger is None:
            return None
        return self._evidence_ledger.evidence_session_id

    @classmethod
    def open_collection(
        cls,
        *,
        run_directory: Path | str,
        repo_root: Path | str,
        connection: object,
    ) -> "EvaluationRunContext":
        return cls._open_collection_impl(
            run_directory=run_directory,
            repo_root=repo_root,
            connection=connection,
            epoch_connection=None,
            allow_test_manifest=False,
        )

    @classmethod
    def _open_collection_for_test(
        cls,
        *,
        run_directory: Path | str,
        repo_root: Path | str,
        connection: object,
        epoch_connection: object | None = None,
    ) -> "EvaluationRunContext":
        return cls._open_collection_impl(
            run_directory=run_directory,
            repo_root=repo_root,
            connection=connection,
            epoch_connection=epoch_connection,
            allow_test_manifest=True,
        )

    @classmethod
    def _open_collection_impl(
        cls,
        *,
        run_directory: Path | str,
        repo_root: Path | str,
        connection: object,
        epoch_connection: object | None,
        allow_test_manifest: bool,
    ) -> "EvaluationRunContext":
        run_dir = Path(run_directory).resolve()
        root = Path(repo_root).resolve()
        manifest = _load_manifest(run_dir)
        if (
            manifest["code_state"].get("git_state_source") != "GIT_VERIFIED"
            and not allow_test_manifest
        ):
            raise RunContextError(
                "collection refuses a test-injected Git state"
            )
        if epoch_connection is not None and epoch_connection is not connection:
            raise RunContextError(
                "epoch and optimizer operations must use the same connection object"
            )
        _verify_artifacts(manifest, root)
        _verify_substrate_source(manifest)
        snapshot = cls._load_bound_snapshot(manifest, root)
        writer_lock = _WriterLock(run_dir / WRITER_LOCK_FILENAME)
        writer_lock.acquire()
        try:
            scope = _scope_argument(manifest["collection_context"]["relation_scope"])
            session = _HypoPGSession(connection)
            active_epoch = session._capture_epoch(scope)
            if active_epoch.get("epoch_hash") != manifest["epoch_hash"]:
                raise RunContextError(
                    "session-bound optimizer epoch differs from the manifest epoch"
                )
            store = CostStore(
                run_dir / "optimizer_responses.csv",
                epoch_hash=str(manifest["epoch_hash"]),
            )
            service = _RevealService(
                store=store,
                session=session,
                relevant_relations=scope,
                max_new_optimizer_calls=manifest["collection_context"][
                    "max_new_optimizer_calls"
                ],
                allow_collection=True,
            )
            return cls(
                run_directory=run_dir,
                repo_root=root,
                manifest=manifest,
                candidate_snapshot=snapshot,
                service=service,
                writer_lock=writer_lock,
                collection_enabled=True,
                evidence_ledger=None,
            )
        except Exception:
            writer_lock.release()
            raise

    @classmethod
    def open_replay(
        cls,
        *,
        run_directory: Path | str,
        repo_root: Path | str,
        evidence_session_id: str,
        seeded_evidence: Sequence[Tuple[QuerySpec, ConfigurationSpec]] = (),
    ) -> "EvaluationRunContext":
        return cls._open_replay_impl(
            run_directory=run_directory,
            repo_root=repo_root,
            evidence_session_id=evidence_session_id,
            seeded_evidence=seeded_evidence,
            allow_test_manifest=False,
        )

    @classmethod
    def _open_replay_for_test(
        cls,
        *,
        run_directory: Path | str,
        repo_root: Path | str,
        evidence_session_id: str,
        seeded_evidence: Sequence[Tuple[QuerySpec, ConfigurationSpec]] = (),
    ) -> "EvaluationRunContext":
        return cls._open_replay_impl(
            run_directory=run_directory,
            repo_root=repo_root,
            evidence_session_id=evidence_session_id,
            seeded_evidence=seeded_evidence,
            allow_test_manifest=True,
        )

    @classmethod
    def _open_replay_impl(
        cls,
        *,
        run_directory: Path | str,
        repo_root: Path | str,
        evidence_session_id: str,
        seeded_evidence: Sequence[Tuple[QuerySpec, ConfigurationSpec]],
        allow_test_manifest: bool,
    ) -> "EvaluationRunContext":
        run_dir = Path(run_directory).resolve()
        root = Path(repo_root).resolve()
        manifest = _load_manifest(run_dir)
        if (
            manifest["code_state"].get("git_state_source") != "GIT_VERIFIED"
            and not allow_test_manifest
        ):
            raise RunContextError("replay refuses a test-injected Git state")
        _verify_artifacts(manifest, root)
        _verify_substrate_source(manifest)
        snapshot = cls._load_bound_snapshot(manifest, root)
        writer_lock = _WriterLock(run_dir / WRITER_LOCK_FILENAME)
        writer_lock.acquire()
        try:
            store = CostStore(
                run_dir / "optimizer_responses.csv",
                epoch_hash=str(manifest["epoch_hash"]),
            )
            service = _RevealService(
                store=store,
                session=None,
                relevant_relations=_scope_argument(
                    manifest["collection_context"]["relation_scope"]
                ),
                max_new_optimizer_calls=manifest["collection_context"][
                    "max_new_optimizer_calls"
                ],
                allow_collection=False,
            )
            ledger = EvidenceLedger(
                run_dir,
                evidence_session_id=evidence_session_id,
                epoch_hash=str(manifest["epoch_hash"]),
            )
            context = cls(
                run_directory=run_dir,
                repo_root=root,
                manifest=manifest,
                candidate_snapshot=snapshot,
                service=service,
                writer_lock=writer_lock,
                collection_enabled=False,
                evidence_ledger=ledger,
            )
            for request in seeded_evidence:
                if (
                    not isinstance(request, tuple)
                    or len(request) != 2
                    or not isinstance(request[0], QuerySpec)
                    or not isinstance(request[1], ConfigurationSpec)
                ):
                    raise TypeError(
                        "seeded_evidence must contain (QuerySpec, ConfigurationSpec) tuples"
                    )
                context._seed_evidence(request[0], request[1])
            return context
        except Exception:
            writer_lock.release()
            raise

    @staticmethod
    def _load_bound_snapshot(
        manifest: Mapping[str, Any], repo_root: Path
    ) -> CandidateSnapshot:
        collection = manifest["collection_context"]
        role = collection["candidate_snapshot_role"]
        record = manifest["input_artifacts"][role]
        path = _artifact_path(record, repo_root)
        tier = CandidateTier.parse(collection["collection_tier"])
        snapshot = (
            load_tier1_candidate_snapshot(path)
            if tier == CandidateTier.TIER1
            else load_tier2_candidate_snapshot(path)
        )
        scope = collection["relation_scope"]
        if scope != "ALL_NON_SYSTEM_RELATIONS":
            covered = {
                (str(item["schema"]), str(item["table"])) for item in scope
            }
            missing = sorted(
                {("public", row.table) for row in snapshot.rows} - covered
            )
            if missing:
                raise RunContextError(
                    "manifest relation scope omits candidate tables: "
                    + ", ".join(f"{schema}.{table}" for schema, table in missing)
                )
        return snapshot

    def close(self) -> None:
        with self._context_lock:
            if self._closed:
                return
            self._writer_lock.release()
            self._closed = True

    def __enter__(self) -> "EvaluationRunContext":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RunContextError("evaluation run context is closed")

    def _require_candidate_membership(
        self, configuration: ConfigurationSpec
    ) -> None:
        outside = [
            index.canonical_text
            for index in configuration.indexes
            if index not in self._allowed_indexes
        ]
        if outside:
            raise RevealError(
                "configuration contains indexes outside the manifest-bound candidate universe: "
                + ", ".join(outside)
            )

    def _require_tier1_identity(
        self, query: QuerySpec, configuration: ConfigurationSpec
    ) -> None:
        if self.collection_tier != CandidateTier.TIER1:
            return
        inventory = self.manifest["collection_context"]["tier1_inventory"]
        queries = {
            (
                item["occurrence_id"],
                item["exact_sql_hash"],
                item["template_id"],
            )
            for item in inventory["queries"]
        }
        configurations = {
            (item["configuration_id"], item["canonical_configuration"])
            for item in inventory["configurations"]
        }
        if (
            query.occurrence_id,
            query.exact_sql_hash,
            query.template_id,
        ) not in queries:
            raise RunContextError("query is outside the manifest-bound Tier-1 inventory")
        if (
            configuration.configuration_id,
            configuration.canonical_json,
        ) not in configurations:
            raise RunContextError(
                "configuration is outside the manifest-bound Tier-1 inventory"
            )

    def _validate_request(
        self, query: QuerySpec, configuration: ConfigurationSpec
    ) -> None:
        self._ensure_open()
        if _load_manifest(self._run_directory) != self._manifest:
            raise RunContextError("manifest changed after the run context was opened")
        _verify_substrate_source(self._manifest)
        _verify_artifacts(self._manifest, self._repo_root)
        if not isinstance(query, QuerySpec) or not isinstance(
            configuration, ConfigurationSpec
        ):
            raise TypeError("reveal requires QuerySpec and ConfigurationSpec")
        self._require_candidate_membership(configuration)
        self._require_tier1_identity(query, configuration)

    def _gate_path(self) -> Path:
        return self.run_directory / "determinism_gate.json"

    def _report_path(self) -> Path:
        return self.run_directory / "determinism_report.md"

    def _gate_binding(self) -> dict[str, Any]:
        collection = self.manifest["collection_context"]
        return {
            "run_id": self.run_id,
            "manifest_checksum": self.manifest["manifest_payload_sha256"],
            "epoch_hash": self.epoch_hash,
            "candidate_snapshot_sha256": collection[
                "candidate_snapshot_sha256"
            ],
        }

    def validate_determinism(
        self, query: QuerySpec, configuration: ConfigurationSpec
    ) -> DeterminismAuthorization:
        """Persist a context-bound three-call gate without returning costs."""

        with self._context_lock:
            self._validate_request(query, configuration)
            if not self._collection_enabled:
                raise RunContextError("replay-only context cannot collect a determinism gate")
            start_calls = self._service.physical_optimizer_calls
            results: list[RevealResult] = []
            error = ""
            try:
                for _ in range(3):
                    results.append(
                        self._service._reveal(query, configuration, _uncached=True)
                    )
            except Exception as exc:
                if isinstance(exc, ConflictingStoredResponseError) and exc.response is not None:
                    results.append(_result_from_stored(exc.response))
                error = f"{type(exc).__name__}: {exc}"
            charged = self._service.physical_optimizer_calls - start_calls
            costs = [result.optimizer_cost for result in results]
            hashes = [result.plan_hash for result in results]
            passed = (
                not error
                and len(results) == 3
                and charged == 3
                and costs[0] == costs[1] == costs[2]
                and hashes[0] == hashes[1] == hashes[2]
                and all(result.epoch_hash == self.epoch_hash for result in results)
            )
            status = "PASS" if passed else "FAIL"
            lines = [
                "# Optimizer Determinism Report",
                "",
                f"- status: `{status}`",
                f"- run_id: `{self.run_id}`",
                f"- manifest_checksum: `{self.manifest['manifest_payload_sha256']}`",
                f"- candidate_snapshot_sha256: `{self.manifest['collection_context']['candidate_snapshot_sha256']}`",
                f"- occurrence_id: `{query.occurrence_id}`",
                f"- exact_sql_hash: `{query.exact_sql_hash}`",
                f"- template_id: `{query.template_id or ''}`",
                f"- configuration_id: `{configuration.configuration_id}`",
                f"- epoch_hash: `{self.epoch_hash}`",
                "- required_charged_measurements: `3`",
                f"- completed_measurements: `{len(results)}`",
                f"- charged_measurements: `{charged}`",
                f"- optimizer_costs: `{json.dumps(costs, separators=(',', ':'))}`",
                f"- plan_hashes: `{json.dumps(hashes, separators=(',', ':'))}`",
            ]
            if error:
                lines.append(f"- error: `{error.replace('`', chr(39))}`")
            lines.extend(["", "Collection may proceed only when status is PASS.", ""])
            _RevealService._write_report(self._report_path(), "\n".join(lines))
            report_sha = sha256_file(self._report_path())
            gate: dict[str, Any] = {
                "gate_schema_version": GATE_SCHEMA_VERSION,
                "status": status,
                **self._gate_binding(),
                "occurrence_id": query.occurrence_id,
                "exact_sql_hash": query.exact_sql_hash,
                "template_id": query.template_id,
                "configuration_id": configuration.configuration_id,
                "charged_measurements": charged,
                "optimizer_costs_sha256": canonical_sha256(costs),
                "canonical_plan_hash": hashes[0] if passed else None,
                "report_sha256": report_sha,
            }
            gate["gate_payload_sha256"] = canonical_sha256(gate)
            _atomic_json(self._gate_path(), gate)
            if not passed:
                raise OptimizerNonDeterminismError(
                    "same context-bound request did not produce exactly three identical responses"
                )
            return self._authorization_from_gate(gate)

    def _authorization_from_gate(
        self, gate: Mapping[str, Any]
    ) -> DeterminismAuthorization:
        return DeterminismAuthorization(
            run_id=str(gate["run_id"]),
            manifest_checksum=str(gate["manifest_checksum"]),
            epoch_hash=str(gate["epoch_hash"]),
            occurrence_id=str(gate["occurrence_id"]),
            exact_sql_hash=str(gate["exact_sql_hash"]),
            template_id=(
                None if gate.get("template_id") is None else str(gate["template_id"])
            ),
            configuration_id=str(gate["configuration_id"]),
            candidate_snapshot_sha256=str(gate["candidate_snapshot_sha256"]),
            charged_measurements=int(gate["charged_measurements"]),
            optimizer_costs_sha256=str(gate["optimizer_costs_sha256"]),
            canonical_plan_hash=str(gate["canonical_plan_hash"]),
            report_sha256=str(gate["report_sha256"]),
        )

    def _require_gate(self) -> DeterminismAuthorization:
        path = self._gate_path()
        try:
            gate = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DeterminismValidationRequired(
                "a persisted context-bound determinism gate is required"
            ) from exc
        except Exception as exc:
            raise DeterminismAuthorizationError(
                f"cannot read determinism gate: {path}: {exc}"
            ) from exc
        if not isinstance(gate, dict):
            raise DeterminismAuthorizationError("determinism gate is not an object")
        recorded_checksum = str(gate.get("gate_payload_sha256", ""))
        payload = dict(gate)
        payload.pop("gate_payload_sha256", None)
        if recorded_checksum != canonical_sha256(payload):
            raise DeterminismAuthorizationError("determinism gate checksum mismatch")
        if gate.get("gate_schema_version") != GATE_SCHEMA_VERSION or gate.get("status") != "PASS":
            raise DeterminismAuthorizationError("determinism gate is not a PASS authorization")
        for key, expected in self._gate_binding().items():
            if gate.get(key) != expected:
                raise DeterminismAuthorizationError(
                    f"determinism gate {key} does not match this context"
                )
        if gate.get("charged_measurements") != 3:
            raise DeterminismAuthorizationError(
                "determinism gate did not record exactly three charged measurements"
            )
        if not self._report_path().is_file() or sha256_file(
            self._report_path()
        ) != gate.get("report_sha256"):
            raise DeterminismAuthorizationError(
                "determinism report is missing or differs from its authorization"
            )
        return self._authorization_from_gate(gate)

    def _public_reveal(
        self, query: QuerySpec, configuration: ConfigurationSpec
    ) -> RevealResult:
        with self._context_lock:
            self._validate_request(query, configuration)
            self._require_gate()
            if self._collection_enabled:
                return self._service._reveal(query, configuration)
            if self._evidence_ledger is None:
                raise RunContextError("replay context has no evidence session")
            key = ExactResponseKey(
                query.exact_sql_hash,
                configuration.configuration_id,
                self.epoch_hash,
            )
            try:
                response = self._service._lookup_ground_truth(query, configuration)
            except MissingOptimizerResponseError:
                self._evidence_ledger.record_reveal(
                    query, key, ground_truth_available=False
                )
                raise
            self._evidence_ledger.record_reveal(
                query, key, ground_truth_available=True
            )
            return _result_from_stored(response)

    def _seed_evidence(
        self, query: QuerySpec, configuration: ConfigurationSpec
    ) -> None:
        """Explicitly seed one replay session with existing exact ground truth."""

        self._validate_request(query, configuration)
        self._require_gate()
        if self._collection_enabled or self._evidence_ledger is None:
            raise RunContextError("evidence can be seeded only in replay mode")
        response = self._service._lookup_ground_truth(query, configuration)
        key = ExactResponseKey(
            response.exact_sql_hash,
            response.configuration_id,
            response.epoch_hash,
        )
        self._evidence_ledger.seed(query, key)

    def collect_tier1(
        self,
        queries: Sequence[QuerySpec],
        configurations: Sequence[ConfigurationSpec],
    ) -> CollectionSummary:
        with self._context_lock:
            if self.collection_tier != CandidateTier.TIER1:
                raise RunContextError("Tier-1 collection requires a Tier-1 context")
            supplied = build_tier1_inventory(list(queries), list(configurations))
            if supplied != self.manifest["collection_context"]["tier1_inventory"]:
                raise RunContextError(
                    "Tier-1 query/configuration inventory differs from the manifest"
                )
            self._require_gate()
            start_calls = self._service.store.physical_optimizer_calls
            start_hits = self._service.store.ground_truth_hits
            requested = 0
            for query in queries:
                for configuration in configurations:
                    self._validate_request(query, configuration)
                    self._service._reveal(query, configuration)
                    requested += 1
            new_calls = self._service.store.physical_optimizer_calls - start_calls
            return CollectionSummary(
                requested_responses=requested,
                physical_optimizer_calls=new_calls,
                evaluator_ground_truth_hits=(
                    self._service.store.ground_truth_hits - start_hits
                ),
            )

    def collect_tier2(
        self,
        requests: Iterable[Tuple[QuerySpec, ConfigurationSpec]],
    ) -> CollectionSummary:
        with self._context_lock:
            if self.collection_tier != CandidateTier.TIER2:
                raise RunContextError("Tier-2 collection requires a Tier-2 context")
            self._require_gate()
            start_calls = self._service.store.physical_optimizer_calls
            start_hits = self._service.store.ground_truth_hits
            requested = 0
            for query, configuration in requests:
                self._validate_request(query, configuration)
                self._service._reveal(query, configuration)
                requested += 1
            new_calls = self._service.store.physical_optimizer_calls - start_calls
            return CollectionSummary(
                requested_responses=requested,
                physical_optimizer_calls=new_calls,
                evaluator_ground_truth_hits=(
                    self._service.store.ground_truth_hits - start_hits
                ),
            )


__all__ = [
    "ArtifactDriftError",
    "DeterminismAuthorization",
    "DeterminismAuthorizationError",
    "EvaluationRunContext",
    "RunContextError",
    "WriterLockError",
    "inspect_writer_lock",
]
