"""Append-only evaluator ground-truth store for Evaluation Substrate v0.

The scientific response identity is exactly ``(exact_sql_hash,
configuration_id, epoch_hash)``. Occurrence and template metadata are retained
as provenance but never enter the response cache key. Policy-visible evidence
is intentionally stored by :mod:`tools.evaluation_substrate.evidence`, not here.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple


RESPONSE_COLUMNS: Tuple[str, ...] = (
    "occurrence_id",
    "exact_sql_hash",
    "template_id",
    "configuration_id",
    "optimizer_cost",
    "used_indexes",
    "plan_hash",
    "physical_optimizer_call",
    "ground_truth_hit",
    "epoch_hash",
    "status",
)

STATUS_OK = "OK"
STATUS_OPTIMIZER_ERROR = "OPTIMIZER_ERROR"
STATUS_MISSING_REJECTED = "MISSING_REJECTED"
STATUS_BUDGET_REJECTED = "BUDGET_REJECTED"
STATUS_EPOCH_MISMATCH = "EPOCH_MISMATCH"
STATUS_EPOCH_UNVERIFIABLE = "EPOCH_UNVERIFIABLE"

VALID_STATUSES = frozenset(
    {
        STATUS_OK,
        STATUS_OPTIMIZER_ERROR,
        STATUS_MISSING_REJECTED,
        STATUS_BUDGET_REJECTED,
        STATUS_EPOCH_MISMATCH,
        STATUS_EPOCH_UNVERIFIABLE,
    }
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONFIGURATION_ID_RE = re.compile(r"^cfg_[0-9a-f]{64}$")


class CostStoreError(RuntimeError):
    """Base class for response-store integrity errors."""


class MissingOptimizerResponseError(CostStoreError):
    """Raised when replay-only access requests an unavailable response."""


class ConflictingStoredResponseError(CostStoreError):
    """Raised when one epoch contains contradictory successful responses."""

    def __init__(
        self,
        message: str,
        *,
        response: Optional["StoredOptimizerResponse"] = None,
    ) -> None:
        self.response = response
        super().__init__(message)


@dataclass(frozen=True)
class StoredOptimizerResponse:
    exact_sql_hash: str
    configuration_id: str
    optimizer_cost: float
    used_indexes: Tuple[str, ...]
    plan_hash: str
    epoch_hash: str

    def scientific_payload(self) -> Tuple[object, ...]:
        return (
            str(self.exact_sql_hash),
            float(self.optimizer_cost),
            tuple(self.used_indexes),
            str(self.plan_hash),
            str(self.epoch_hash),
        )

    @property
    def query_hash(self) -> str:
        """Temporary compatibility alias for ``exact_sql_hash``."""

        return self.exact_sql_hash


def _flag(value: object, *, field: str) -> int:
    text = "" if value is None else str(value).strip()
    if text not in {"0", "1"}:
        raise CostStoreError(f"{field} must be 0 or 1, got {value!r}")
    return int(text)


def _sha256(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not _SHA256_RE.fullmatch(text):
        raise CostStoreError(f"{field} must be a 64-character SHA-256 hex digest")
    return text.lower()


def _used_indexes_json(indexes: Iterable[str]) -> str:
    normalized = sorted({str(index).strip() for index in indexes if str(index).strip()})
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _parse_used_indexes(value: object) -> Tuple[str, ...]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception as exc:  # pragma: no cover - exact parser exception is irrelevant
        raise CostStoreError(f"invalid used_indexes JSON: {value!r}") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise CostStoreError("used_indexes must be a JSON list of strings")
    normalized = tuple(sorted({item.strip() for item in parsed if item.strip()}))
    return normalized


class CostStore:
    """Persist reveal events and expose an epoch-scoped exact-response cache."""

    def __init__(self, path: Path | str, *, epoch_hash: str) -> None:
        self.path = Path(path)
        self.epoch_hash = _sha256(epoch_hash, field="epoch_hash")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[Tuple[str, str], StoredOptimizerResponse] = {}
        self._occurrences: Dict[str, Tuple[str, Optional[str]]] = {}
        self._conflicting_keys: set[Tuple[str, str]] = set()
        self.physical_optimizer_calls = 0
        self.ground_truth_hits = 0
        self.stale_epoch_rows = 0
        self.event_count = 0

        if self.path.exists() and self.path.stat().st_size > 0:
            self._load_existing()
        else:
            with self.path.open("w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=list(RESPONSE_COLUMNS)).writeheader()
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass

    @staticmethod
    def _key(exact_sql_hash: object, configuration_id: object) -> Tuple[str, str]:
        sql_hash = _sha256(exact_sql_hash, field="exact_sql_hash")
        cid = str(configuration_id).strip()
        if not cid:
            raise ValueError("exact_sql_hash and configuration_id are required")
        if not _CONFIGURATION_ID_RE.fullmatch(cid):
            raise CostStoreError("configuration_id is not a v0 canonical configuration ID")
        return sql_hash, cid

    def _bind_occurrence(
        self,
        occurrence_id: object,
        exact_sql_hash: object,
        template_id: object = None,
    ) -> Tuple[str, str, Optional[str]]:
        occurrence = str(occurrence_id or "").strip()
        if not occurrence:
            raise CostStoreError("occurrence_id is required")
        if any(ord(character) < 32 or ord(character) == 127 for character in occurrence):
            raise CostStoreError("occurrence_id contains a control character")
        sql_hash = _sha256(exact_sql_hash, field="exact_sql_hash")
        template = None if template_id is None else str(template_id).strip()
        if template == "":
            raise CostStoreError("template_id cannot be empty when supplied")
        binding = (sql_hash, template)
        existing = self._occurrences.get(occurrence)
        if existing is not None and existing != binding:
            raise CostStoreError(
                f"occurrence_id {occurrence!r} is associated with conflicting exact SQL or template metadata"
            )
        self._occurrences[occurrence] = binding
        return occurrence, sql_hash, template

    @property
    def charged_optimizer_calls(self) -> int:
        """Compatibility alias; this counter means physical optimizer calls only."""

        return self.physical_optimizer_calls

    def _response_from_row(self, row: dict[str, str]) -> StoredOptimizerResponse:
        try:
            cost = float(row.get("optimizer_cost", ""))
        except Exception as exc:
            raise CostStoreError("successful response has invalid optimizer_cost") from exc
        if not math.isfinite(cost) or cost < 0:
            raise CostStoreError(
                "successful response optimizer_cost must be finite and non-negative"
            )
        plan_hash = _sha256(row.get("plan_hash", ""), field="plan_hash")
        sql_hash, cid = self._key(
            row.get("exact_sql_hash", ""), row.get("configuration_id", "")
        )
        return StoredOptimizerResponse(
            exact_sql_hash=sql_hash,
            configuration_id=cid,
            optimizer_cost=cost,
            used_indexes=_parse_used_indexes(row.get("used_indexes", "[]")),
            plan_hash=plan_hash,
            epoch_hash=_sha256(row.get("epoch_hash", ""), field="epoch_hash"),
        )

    def _cache_response(self, response: StoredOptimizerResponse, *, allow_conflict: bool) -> None:
        key = self._key(response.exact_sql_hash, response.configuration_id)
        existing = self._cache.get(key)
        if existing is not None and existing.scientific_payload() != response.scientific_payload():
            self._conflicting_keys.add(key)
            if not allow_conflict:
                raise ConflictingStoredResponseError(
                    f"conflicting successful responses for exact_sql_hash={key[0]!r}, configuration={key[1]!r}",
                    response=response,
                )
            return
        if key not in self._conflicting_keys:
            self._cache[key] = response

    def _load_existing(self) -> None:
        with self.path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if tuple(reader.fieldnames or ()) != RESPONSE_COLUMNS:
                raise CostStoreError(
                    f"unexpected optimizer response schema in {self.path}: {reader.fieldnames!r}"
                )
            for row in reader:
                self.event_count += 1
                _occurrence, sql_hash, _template = self._bind_occurrence(
                    row.get("occurrence_id", ""),
                    row.get("exact_sql_hash", ""),
                    row.get("template_id") or None,
                )
                self._key(sql_hash, row.get("configuration_id", ""))
                row_epoch = _sha256(row.get("epoch_hash", ""), field="epoch_hash")
                physical = _flag(
                    row.get("physical_optimizer_call", ""),
                    field="physical_optimizer_call",
                )
                ground_truth_hit = _flag(
                    row.get("ground_truth_hit", ""), field="ground_truth_hit"
                )
                status = str(row.get("status", "")).strip()
                if status not in VALID_STATUSES:
                    raise CostStoreError(f"unsupported response status: {status!r}")
                used_indexes = _parse_used_indexes(row.get("used_indexes", "[]"))
                cost_text = str(row.get("optimizer_cost", "")).strip()
                plan_text = str(row.get("plan_hash", "")).strip()
                if status == STATUS_OK:
                    if physical + ground_truth_hit != 1:
                        raise CostStoreError(
                            "OK response must be exactly one physical call or one evaluator ground-truth hit"
                        )
                    self._response_from_row(row)
                else:
                    if ground_truth_hit:
                        raise CostStoreError(
                            "a rejected or failed event cannot be a ground-truth hit"
                        )
                    if cost_text or used_indexes or plan_text:
                        raise CostStoreError(
                            "a rejected or failed event cannot contain a scientific response"
                        )
                    if status in {STATUS_MISSING_REJECTED, STATUS_BUDGET_REJECTED} and physical:
                        raise CostStoreError(f"{status} must be rejected before a physical call")
                    if status == STATUS_OPTIMIZER_ERROR and not physical:
                        raise CostStoreError("OPTIMIZER_ERROR must record its physical call")

                # A run guard accounts for every optimizer call admitted in this
                # append-only run log.  Epoch scope controls cache eligibility,
                # never whether an already-spent interaction is counted.
                self.physical_optimizer_calls += physical
                self.ground_truth_hits += ground_truth_hit
                if row_epoch != self.epoch_hash:
                    self.stale_epoch_rows += 1
                    continue
                if status == STATUS_OK:
                    self._cache_response(self._response_from_row(row), allow_conflict=False)

    def lookup(
        self,
        occurrence_id: object,
        configuration_id: object,
        *,
        exact_sql_hash: object,
        template_id: object = None,
    ) -> Optional[StoredOptimizerResponse]:
        _occurrence, sql_hash, _template = self._bind_occurrence(
            occurrence_id, exact_sql_hash, template_id
        )
        key = self._key(sql_hash, configuration_id)
        if key in self._conflicting_keys:
            raise ConflictingStoredResponseError(
                f"response cache is contradictory for exact_sql_hash={key[0]!r}, configuration={key[1]!r}"
            )
        return self._cache.get(key)

    def require(
        self,
        occurrence_id: object,
        configuration_id: object,
        *,
        exact_sql_hash: object,
        template_id: object = None,
    ) -> StoredOptimizerResponse:
        response = self.lookup(
            occurrence_id,
            configuration_id,
            exact_sql_hash=exact_sql_hash,
            template_id=template_id,
        )
        if response is None:
            raise MissingOptimizerResponseError(
                f"missing exact response for exact_sql_hash={str(exact_sql_hash)!r}, configuration={str(configuration_id)!r}, "
                f"epoch={self.epoch_hash}"
            )
        return response

    def append_event(
        self,
        *,
        occurrence_id: object,
        exact_sql_hash: object,
        template_id: object = None,
        configuration_id: object,
        optimizer_cost: Optional[float],
        used_indexes: Sequence[str] = (),
        plan_hash: str = "",
        physical_optimizer_call: int,
        ground_truth_hit: int,
        status: str,
        epoch_hash: Optional[str] = None,
    ) -> Optional[StoredOptimizerResponse]:
        occurrence, normalized_sql_hash, template = self._bind_occurrence(
            occurrence_id, exact_sql_hash, template_id
        )
        _sql_hash, cid = self._key(normalized_sql_hash, configuration_id)
        event_epoch = _sha256(
            epoch_hash if epoch_hash is not None else self.epoch_hash,
            field="epoch_hash",
        )
        physical = _flag(
            physical_optimizer_call, field="physical_optimizer_call"
        )
        ground_truth = _flag(ground_truth_hit, field="ground_truth_hit")
        status_text = str(status).strip()
        if status_text not in VALID_STATUSES:
            raise CostStoreError(f"unsupported response status: {status_text!r}")
        if ground_truth and physical:
            raise CostStoreError(
                "an evaluator ground-truth hit cannot also be a physical optimizer call"
            )

        response: Optional[StoredOptimizerResponse] = None
        cost_text = ""
        used_text = _used_indexes_json(used_indexes)
        plan_text = str(plan_hash).strip()
        if status_text == STATUS_OK:
            if physical + ground_truth != 1:
                raise CostStoreError(
                    "OK response must be exactly one physical call or one evaluator ground-truth hit"
                )
            if (
                optimizer_cost is None
                or not math.isfinite(float(optimizer_cost))
                or float(optimizer_cost) < 0
            ):
                raise CostStoreError(
                    "OK response requires a finite, non-negative optimizer_cost"
                )
            plan_text = _sha256(plan_text, field="plan_hash")
            cost_text = format(float(optimizer_cost), ".17g")
            response = StoredOptimizerResponse(
                exact_sql_hash=normalized_sql_hash,
                configuration_id=cid,
                optimizer_cost=float(optimizer_cost),
                used_indexes=tuple(json.loads(used_text)),
                plan_hash=plan_text,
                epoch_hash=event_epoch,
            )
        else:
            if ground_truth:
                raise CostStoreError(
                    "a rejected or failed event cannot be a ground-truth hit"
                )
            if optimizer_cost is not None or tuple(used_indexes) or plan_text:
                raise CostStoreError(
                    "a rejected or failed event cannot contain a scientific response"
                )
            if status_text in {STATUS_MISSING_REJECTED, STATUS_BUDGET_REJECTED} and physical:
                raise CostStoreError(
                    f"{status_text} must be rejected before a physical call"
                )
            if status_text == STATUS_OPTIMIZER_ERROR and not physical:
                raise CostStoreError("OPTIMIZER_ERROR must record its physical call")

        row = {
            "occurrence_id": occurrence,
            "exact_sql_hash": normalized_sql_hash,
            "template_id": template or "",
            "configuration_id": cid,
            "optimizer_cost": cost_text,
            "used_indexes": used_text,
            "plan_hash": plan_text,
            "physical_optimizer_call": str(physical),
            "ground_truth_hit": str(ground_truth),
            "epoch_hash": event_epoch,
            "status": status_text,
        }
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=list(RESPONSE_COLUMNS)).writerow(row)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

        self.event_count += 1
        self.physical_optimizer_calls += physical
        self.ground_truth_hits += ground_truth
        if event_epoch == self.epoch_hash:
            if response is not None:
                self._cache_response(response, allow_conflict=False)
        else:
            self.stale_epoch_rows += 1
        return response

    def record_ground_truth_hit(
        self,
        response: StoredOptimizerResponse,
        *,
        occurrence_id: object,
        template_id: object = None,
    ) -> StoredOptimizerResponse:
        persisted = self.append_event(
            occurrence_id=occurrence_id,
            exact_sql_hash=response.exact_sql_hash,
            template_id=template_id,
            configuration_id=response.configuration_id,
            optimizer_cost=response.optimizer_cost,
            used_indexes=response.used_indexes,
            plan_hash=response.plan_hash,
            physical_optimizer_call=0,
            ground_truth_hit=1,
            epoch_hash=response.epoch_hash,
            status=STATUS_OK,
        )
        assert persisted is not None
        return persisted
