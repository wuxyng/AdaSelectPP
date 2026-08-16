"""Workload-occurrence and policy-evidence ledgers for substrate replay.

These ledgers are deliberately separate from ``optimizer_responses.csv``.
Ground truth may exist without any evidence session having observed it.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from .schema import QuerySpec


OCCURRENCE_COLUMNS: Tuple[str, ...] = (
    "occurrence_id",
    "exact_sql_hash",
    "template_id",
)

EVIDENCE_COLUMNS: Tuple[str, ...] = (
    "evidence_session_id",
    "occurrence_id",
    "exact_sql_hash",
    "configuration_id",
    "epoch_hash",
    "status",
    "charged_policy_probe",
    "evidence_hit",
)

EVIDENCE_OK = "OK"
EVIDENCE_MISSING_REJECTED = "MISSING_REJECTED"
EVIDENCE_SEEDED = "SEEDED"
_VALID_EVIDENCE_STATUSES = frozenset(
    {EVIDENCE_OK, EVIDENCE_MISSING_REJECTED, EVIDENCE_SEEDED}
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONFIGURATION_ID_RE = re.compile(r"^cfg_[0-9a-f]{64}$")


class EvidenceStoreError(RuntimeError):
    """Raised when occurrence or evidence-session state is inconsistent."""


def _required_text(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceStoreError(f"{field} is required")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise EvidenceStoreError(f"{field} contains a control character")
    return text


def _sha256(value: object, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise EvidenceStoreError(f"{field} must be a 64-character SHA-256 digest")
    return text


def _flag(value: object, *, field: str) -> int:
    text = str(value).strip()
    if text not in {"0", "1"}:
        raise EvidenceStoreError(f"{field} must be 0 or 1")
    return int(text)


def _ensure_csv(path: Path, columns: Tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=list(columns)).writeheader()
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass


@dataclass(frozen=True)
class ExactResponseKey:
    """Identity of one exact evaluator ground-truth response."""

    exact_sql_hash: str
    configuration_id: str
    epoch_hash: str

    def __post_init__(self) -> None:
        exact_sql_hash = _sha256(self.exact_sql_hash, field="exact_sql_hash")
        configuration_id = str(self.configuration_id or "").strip()
        if not _CONFIGURATION_ID_RE.fullmatch(configuration_id):
            raise EvidenceStoreError(
                "configuration_id is not a v0 canonical configuration ID"
            )
        epoch_hash = _sha256(self.epoch_hash, field="epoch_hash")
        object.__setattr__(self, "exact_sql_hash", exact_sql_hash)
        object.__setattr__(self, "configuration_id", configuration_id)
        object.__setattr__(self, "epoch_hash", epoch_hash)

    def as_tuple(self) -> Tuple[str, str, str]:
        return (self.exact_sql_hash, self.configuration_id, self.epoch_hash)


@dataclass(frozen=True)
class EvidenceEvent:
    evidence_session_id: str
    occurrence_id: str
    response_key: ExactResponseKey
    status: str
    charged_policy_probe: int
    evidence_hit: int


class EvidenceLedger:
    """Append-only evidence accounting for one named replay session."""

    def __init__(
        self,
        run_directory: Path | str,
        *,
        evidence_session_id: object,
        epoch_hash: object,
    ) -> None:
        self.run_directory = Path(run_directory)
        self.evidence_session_id = _required_text(
            evidence_session_id, field="evidence_session_id"
        )
        self.epoch_hash = _sha256(epoch_hash, field="epoch_hash")
        self.occurrence_path = self.run_directory / "workload_occurrences.csv"
        self.event_path = self.run_directory / "evidence_events.csv"
        self._occurrences: Dict[str, Tuple[str, Optional[str]]] = {}
        self._seen: set[ExactResponseKey] = set()
        self.charged_policy_probes = 0
        self.event_count = 0
        _ensure_csv(self.occurrence_path, OCCURRENCE_COLUMNS)
        _ensure_csv(self.event_path, EVIDENCE_COLUMNS)
        self._load_occurrences()
        self._load_events()

    @property
    def occurrence_count(self) -> int:
        return len(self._occurrences)

    def _load_occurrences(self) -> None:
        with self.occurrence_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != OCCURRENCE_COLUMNS:
                raise EvidenceStoreError("unexpected workload occurrence schema")
            for row in reader:
                occurrence = _required_text(
                    row.get("occurrence_id"), field="occurrence_id"
                )
                sql_hash = _sha256(
                    row.get("exact_sql_hash"), field="exact_sql_hash"
                )
                template = str(row.get("template_id") or "").strip() or None
                binding = (sql_hash, template)
                if occurrence in self._occurrences:
                    raise EvidenceStoreError(
                        f"duplicate persisted occurrence_id {occurrence!r}"
                    )
                self._occurrences[occurrence] = binding

    def _load_events(self) -> None:
        seen_by_session: Dict[str, set[ExactResponseKey]] = {}
        with self.event_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != EVIDENCE_COLUMNS:
                raise EvidenceStoreError("unexpected evidence event schema")
            for row in reader:
                session = _required_text(
                    row.get("evidence_session_id"), field="evidence_session_id"
                )
                occurrence = _required_text(
                    row.get("occurrence_id"), field="occurrence_id"
                )
                key = ExactResponseKey(
                    row.get("exact_sql_hash", ""),
                    row.get("configuration_id", ""),
                    row.get("epoch_hash", ""),
                )
                if occurrence not in self._occurrences:
                    raise EvidenceStoreError(
                        f"evidence event references unknown occurrence {occurrence!r}"
                    )
                if self._occurrences[occurrence][0] != key.exact_sql_hash:
                    raise EvidenceStoreError(
                        "evidence event exact SQL conflicts with its occurrence binding"
                    )
                status = str(row.get("status") or "").strip()
                if status not in _VALID_EVIDENCE_STATUSES:
                    raise EvidenceStoreError(f"unsupported evidence status {status!r}")
                charged = _flag(
                    row.get("charged_policy_probe"), field="charged_policy_probe"
                )
                hit = _flag(row.get("evidence_hit"), field="evidence_hit")
                session_seen = seen_by_session.setdefault(session, set())
                if status == EVIDENCE_SEEDED:
                    if charged or hit or key in session_seen:
                        raise EvidenceStoreError("invalid or duplicate SEEDED evidence event")
                    session_seen.add(key)
                elif status == EVIDENCE_OK:
                    expected_hit = int(key in session_seen)
                    if hit != expected_hit or charged != 1 - expected_hit:
                        raise EvidenceStoreError(
                            "evidence accounting disagrees with prior session evidence"
                        )
                    session_seen.add(key)
                elif charged != 1 or hit:
                    raise EvidenceStoreError(
                        "missing ground truth must be a charged non-hit and cannot seed evidence"
                    )
                if session == self.evidence_session_id:
                    self.event_count += 1
                    self.charged_policy_probes += charged
        self._seen = seen_by_session.get(self.evidence_session_id, set())

    def bind_occurrence(self, query: QuerySpec) -> None:
        if not isinstance(query, QuerySpec):
            raise TypeError("occurrence binding requires QuerySpec")
        binding = (query.exact_sql_hash, query.template_id)
        existing = self._occurrences.get(query.occurrence_id)
        if existing is not None:
            if existing != binding:
                raise EvidenceStoreError(
                    f"occurrence_id {query.occurrence_id!r} cannot be rebound"
                )
            return
        row = {
            "occurrence_id": query.occurrence_id,
            "exact_sql_hash": query.exact_sql_hash,
            "template_id": query.template_id or "",
        }
        with self.occurrence_path.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=list(OCCURRENCE_COLUMNS)).writerow(row)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        self._occurrences[query.occurrence_id] = binding

    def _validate_request(self, query: QuerySpec, key: ExactResponseKey) -> None:
        self.bind_occurrence(query)
        if query.exact_sql_hash != key.exact_sql_hash:
            raise EvidenceStoreError("response key does not match occurrence exact SQL")
        if key.epoch_hash != self.epoch_hash:
            raise EvidenceStoreError("response key does not match evidence-session epoch")

    def _append(
        self,
        query: QuerySpec,
        key: ExactResponseKey,
        *,
        status: str,
        charged: int,
        hit: int,
    ) -> EvidenceEvent:
        row = {
            "evidence_session_id": self.evidence_session_id,
            "occurrence_id": query.occurrence_id,
            "exact_sql_hash": key.exact_sql_hash,
            "configuration_id": key.configuration_id,
            "epoch_hash": key.epoch_hash,
            "status": status,
            "charged_policy_probe": str(charged),
            "evidence_hit": str(hit),
        }
        with self.event_path.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=list(EVIDENCE_COLUMNS)).writerow(row)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        self.event_count += 1
        self.charged_policy_probes += charged
        return EvidenceEvent(
            evidence_session_id=self.evidence_session_id,
            occurrence_id=query.occurrence_id,
            response_key=key,
            status=status,
            charged_policy_probe=charged,
            evidence_hit=hit,
        )

    def record_reveal(
        self,
        query: QuerySpec,
        key: ExactResponseKey,
        *,
        ground_truth_available: bool,
    ) -> EvidenceEvent:
        self._validate_request(query, key)
        if not ground_truth_available:
            return self._append(
                query,
                key,
                status=EVIDENCE_MISSING_REJECTED,
                charged=1,
                hit=0,
            )
        hit = int(key in self._seen)
        event = self._append(
            query,
            key,
            status=EVIDENCE_OK,
            charged=1 - hit,
            hit=hit,
        )
        self._seen.add(key)
        return event

    def seed(self, query: QuerySpec, key: ExactResponseKey) -> EvidenceEvent | None:
        """Explicitly seed one known response key without charging a policy probe."""

        self._validate_request(query, key)
        if key in self._seen:
            return None
        event = self._append(
            query,
            key,
            status=EVIDENCE_SEEDED,
            charged=0,
            hit=0,
        )
        self._seen.add(key)
        return event


__all__ = [
    "EVIDENCE_COLUMNS",
    "EVIDENCE_MISSING_REJECTED",
    "EVIDENCE_OK",
    "EVIDENCE_SEEDED",
    "EvidenceEvent",
    "EvidenceLedger",
    "EvidenceStoreError",
    "ExactResponseKey",
    "OCCURRENCE_COLUMNS",
]
