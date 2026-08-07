"""Only optimizer-access path for Evaluation Substrate v0.

The module-level ``reveal(context, query, configuration)`` function is the
public accounting boundary. Collection counts physical HypoPG calls; replay
counts first-time policy evidence probes in a separate evidence ledger.
Candidate generation, ranking, selection, transition logic, execution timing,
and DML costing are intentionally absent.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from tools.evaluation_substrate.cost_store import (
    STATUS_BUDGET_REJECTED,
    STATUS_EPOCH_MISMATCH,
    STATUS_EPOCH_UNVERIFIABLE,
    STATUS_MISSING_REJECTED,
    STATUS_OK,
    STATUS_OPTIMIZER_ERROR,
    ConflictingStoredResponseError,
    CostStore,
    MissingOptimizerResponseError,
    StoredOptimizerResponse,
)
from tools.evaluation_substrate.schema import (
    CandidateSnapshot,
    CandidateTier,
    ConfigurationSpec,
    IndexDefinition,
    QuerySpec,
)
from tools.evaluation_substrate.epoch_fingerprint import collect_epoch_fingerprint


class RevealError(RuntimeError):
    """Base class for fail-closed reveal errors."""


class OptimizerCallBudgetExceeded(RevealError):
    """Raised before an optimizer call that would exceed the configured guard."""


class OptimizerRevealError(RevealError):
    """Raised after a charged optimizer call fails."""


class EpochMismatchError(RevealError):
    """Raised when the current database epoch differs from the bound epoch."""

    def __init__(self, *, bound_epoch: str, current_epoch: str) -> None:
        self.bound_epoch = str(bound_epoch)
        self.current_epoch = str(current_epoch)
        super().__init__(
            f"database epoch changed: bound={self.bound_epoch}, current={self.current_epoch}"
        )


class OptimizerNonDeterminismError(RevealError):
    """Raised when three identical optimizer requests do not agree exactly."""


class DeterminismValidationRequired(RevealError):
    """Raised when bulk collection starts before its three-call gate."""


@dataclass(frozen=True)
class RevealResult:
    """The deliberately narrow scientific result returned to callers."""

    optimizer_cost: float
    used_indexes: Tuple[str, ...]
    plan_hash: str
    epoch_hash: str


@dataclass(frozen=True)
class OptimizerEvaluation:
    optimizer_cost: float
    used_indexes: Tuple[str, ...]
    plan_hash: str

    def __post_init__(self) -> None:
        cost = float(self.optimizer_cost)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("optimizer_cost must be finite and non-negative")
        if isinstance(self.used_indexes, (str, bytes)):
            raise ValueError("used_indexes must be a sequence of index identities")
        if any(not isinstance(value, str) for value in self.used_indexes):
            raise ValueError("used_indexes must contain only strings")
        indexes = tuple(sorted({str(value).strip() for value in self.used_indexes}))
        if any(not value for value in indexes):
            raise ValueError("used_indexes cannot contain an empty identity")
        plan_hash = str(self.plan_hash).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
            raise ValueError("plan_hash must be a 64-character SHA-256 hex digest")
        object.__setattr__(self, "optimizer_cost", cost)
        object.__setattr__(self, "used_indexes", indexes)
        object.__setattr__(self, "plan_hash", plan_hash)


@dataclass(frozen=True)
class CollectionSummary:
    requested_responses: int
    physical_optimizer_calls: int
    evaluator_ground_truth_hits: int
    charged_policy_probes: int = 0

    @property
    def new_optimizer_calls(self) -> int:
        """Compatibility alias for physical collection calls."""

        return self.physical_optimizer_calls

    @property
    def cache_hits(self) -> int:
        """Compatibility alias for evaluator ground-truth hits, not evidence hits."""

        return self.evaluator_ground_truth_hits


class _OptimizerSession(Protocol):
    connection: object

    def _evaluate(
        self, query: QuerySpec, configuration: ConfigurationSpec
    ) -> OptimizerEvaluation:
        """Perform exactly one optimizer EXPLAIN under `configuration`."""

    def _capture_epoch(
        self, relevant_relations: Optional[Iterable[object]]
    ) -> Mapping[str, object]:
        """Capture an epoch from the same session used by ``_evaluate``."""


_READ_QUERY_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN_QUERY_TOKEN_RE = re.compile(
    r"\b(insert|update|delete|merge|copy|call|create|alter|drop|truncate|grant|revoke|"
    r"vacuum|analyze|refresh|reindex|cluster|do|explain)\b",
    re.IGNORECASE,
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _quote_ident(identifier: str) -> str:
    value = str(identifier)
    if not value or "\x00" in value:
        raise ValueError(f"invalid PostgreSQL identifier: {identifier!r}")
    return '"' + value.replace('"', '""') + '"'


def _index_text(index: IndexDefinition) -> str:
    canonical = getattr(index, "canonical_text", None)
    if callable(canonical):
        return str(canonical())
    if isinstance(canonical, str):
        return canonical
    return f"{index.table}({','.join(index.columns)})"


def _safe_explain_query(sql: str) -> str:
    text = str(sql or "").strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not text or not _READ_QUERY_RE.match(text):
        raise RevealError("only frozen SELECT/WITH workload statements may be explained")
    if ";" in text:
        raise RevealError("multiple SQL statements are forbidden")
    # In particular, reject data-modifying CTEs.  This lexical guard is
    # deliberately conservative: a frozen workload query that cannot be
    # established as read-only is rejected rather than guessed.
    if _FORBIDDEN_QUERY_TOKEN_RE.search(text):
        raise RevealError("optimizer access is restricted to read-only SELECT/WITH queries")
    return text


def _normalize_plan_value(value: object, index_name_map: Mapping[str, str], oid_map: Mapping[str, str]) -> object:
    if isinstance(value, dict):
        out = {}
        for key in sorted(value):
            item = value[key]
            if key == "Index Name" and isinstance(item, str):
                replacement = index_name_map.get(item)
                if replacement is None:
                    replacement = next(
                        (canonical for oid, canonical in oid_map.items() if f"<{oid}>" in item),
                        f"physical:{item}",
                    )
                out[key] = replacement
            else:
                out[key] = _normalize_plan_value(item, index_name_map, oid_map)
        return out
    if isinstance(value, list):
        return [_normalize_plan_value(item, index_name_map, oid_map) for item in value]
    if isinstance(value, tuple):
        return [_normalize_plan_value(item, index_name_map, oid_map) for item in value]
    return value


def _used_indexes(plan: object) -> Tuple[str, ...]:
    found: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            name = value.get("Index Name")
            if isinstance(name, str) and name.strip():
                found.add(name.strip())
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(plan)
    return tuple(sorted(found))


class _HypoPGSession:
    """Narrow DB-API adapter: HypoPG reset/install/verify plus one EXPLAIN.

    Hypothetical `CREATE INDEX` text is always bound as data to
    `hypopg_create_index(%s)`; this class has no physical DDL path and exposes no
    generic SQL executor.
    """

    def __init__(self, connection) -> None:
        self._connection = connection

    def _commit(self) -> None:
        commit = getattr(self._connection, "commit", None)
        if callable(commit):
            commit()

    def _rollback(self) -> None:
        rollback = getattr(self._connection, "rollback", None)
        if callable(rollback):
            rollback()

    def _fetchone(self, statement: str, params: Optional[tuple] = None):
        try:
            with self._connection.cursor() as cursor:
                if params is None:
                    cursor.execute(statement)
                else:
                    cursor.execute(statement, params)
                row = cursor.fetchone()
            self._commit()
            return row
        except Exception:
            self._rollback()
            raise

    def _fetchall(self, statement: str, params: Optional[tuple] = None):
        try:
            with self._connection.cursor() as cursor:
                if params is None:
                    cursor.execute(statement)
                else:
                    cursor.execute(statement, params)
                rows = cursor.fetchall()
            self._commit()
            return rows
        except Exception:
            self._rollback()
            raise

    def _list_hypopg_state(self) -> Tuple[Tuple[str, str], ...]:
        errors: list[str] = []
        for statement in (
            "SELECT indexrelid::text, hypopg_get_indexdef(indexrelid)::text "
            "FROM hypopg_list_indexes ORDER BY indexrelid",
            "SELECT indexrelid::text, hypopg_get_indexdef(indexrelid)::text "
            "FROM hypopg() ORDER BY indexrelid",
        ):
            try:
                rows = self._fetchall(statement)
                return tuple((str(row[0]), str(row[1])) for row in rows)
            except Exception as exc:  # pragma: no cover - depends on HypoPG version
                errors.append(f"{type(exc).__name__}: {exc}")
        raise OptimizerRevealError("cannot enumerate HypoPG indexes: " + " | ".join(errors))

    def _reset_verified(self) -> None:
        self._fetchone("SELECT hypopg_reset()")
        if self._list_hypopg_state():
            raise OptimizerRevealError("hypopg_reset() did not leave an empty session-local index set")

    def _assert_empty(self) -> None:
        if self._list_hypopg_state():
            raise OptimizerRevealError(
                "HypoPG state must be empty at an epoch boundary"
            )

    def _capture_epoch(
        self, relevant_relations: Optional[Iterable[object]]
    ) -> Mapping[str, object]:
        self._assert_empty()
        epoch = collect_epoch_fingerprint(self._connection, relevant_relations)
        self._assert_empty()
        return epoch

    @staticmethod
    def _definition(index: IndexDefinition) -> str:
        columns = ", ".join(_quote_ident(column) for column in index.columns)
        return f"CREATE INDEX ON {_quote_ident('public')}.{_quote_ident(index.table)} ({columns})"

    @staticmethod
    def _parse_installed_definition(definition: str) -> IndexDefinition:
        identifier = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)'
        match = re.fullmatch(
            rf"\s*CREATE\s+INDEX\s+ON\s+"
            rf"(?:(?P<schema>{identifier})\.)?"
            rf"(?P<table>{identifier})\s+"
            rf"(?:USING\s+btree\s+)?"
            rf"\((?P<columns>{identifier}(?:\s*,\s*{identifier})*)\)\s*",
            str(definition),
            flags=re.IGNORECASE,
        )
        if match is None:
            raise OptimizerRevealError(
                f"installed HypoPG definition is outside the v0 simple-btree grammar: {definition!r}"
            )

        def unquote(token: str) -> str:
            value = token.strip()
            if value.startswith('"') and value.endswith('"'):
                return value[1:-1].replace('""', '"')
            return value

        schema = unquote(match.group("schema") or "public")
        if schema != "public":
            raise OptimizerRevealError(
                f"installed HypoPG index resolved outside public schema: {definition!r}"
            )
        return IndexDefinition(
            unquote(match.group("table")),
            tuple(unquote(value) for value in match.group("columns").split(",")),
        )

    @staticmethod
    def _plan_payload(raw: object) -> Mapping[str, object]:
        value = raw
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, list):
            if len(value) != 1 or not isinstance(value[0], dict):
                raise OptimizerRevealError("unexpected EXPLAIN JSON list payload")
            value = value[0]
        if not isinstance(value, dict):
            raise OptimizerRevealError("unexpected EXPLAIN JSON payload")
        if "Plan" not in value or not isinstance(value["Plan"], dict):
            raise OptimizerRevealError("EXPLAIN JSON payload is missing the root Plan")
        return value

    def _evaluate(
        self, query: QuerySpec, configuration: ConfigurationSpec
    ) -> OptimizerEvaluation:
        sql = _safe_explain_query(query.sql)
        name_map: dict[str, str] = {}
        oid_map: dict[str, str] = {}
        expected_oids: list[str] = []
        primary_error: Optional[BaseException] = None
        try:
            self._reset_verified()
            for index in configuration.indexes:
                row = self._fetchone(
                    "SELECT * FROM hypopg_create_index(%s)",
                    (self._definition(index),),
                )
                if not row or row[0] is None:
                    raise OptimizerRevealError(f"HypoPG did not return an OID for {_index_text(index)}")
                oid = str(row[0])
                canonical = f"hypopg:{_index_text(index)}"
                expected_oids.append(oid)
                oid_map[oid] = canonical
                if len(row) > 1 and row[1] is not None:
                    name_map[str(row[1])] = canonical

            installed_state = self._list_hypopg_state()
            installed_oids = tuple(oid for oid, _definition in installed_state)
            if tuple(sorted(installed_oids)) != tuple(sorted(expected_oids)):
                raise OptimizerRevealError(
                    "installed HypoPG OIDs differ from the requested configuration: "
                    f"requested={sorted(expected_oids)!r}, installed={sorted(installed_oids)!r}"
                )
            installed_definitions = tuple(
                sorted(self._parse_installed_definition(definition) for _oid, definition in installed_state)
            )
            if installed_definitions != tuple(sorted(configuration.indexes)):
                raise OptimizerRevealError(
                    "authoritative installed HypoPG definitions differ from the requested "
                    f"configuration: requested={configuration.canonical_text!r}, "
                    f"installed={[index.canonical_text for index in installed_definitions]!r}"
                )

            row = self._fetchone(f"EXPLAIN (FORMAT JSON) {sql}")
            if not row:
                raise OptimizerRevealError("EXPLAIN returned no row")
            payload = self._plan_payload(row[0])
            normalized = _normalize_plan_value(payload, name_map, oid_map)
            root = normalized["Plan"]
            try:
                optimizer_cost = float(root["Total Cost"])
            except Exception as exc:
                raise OptimizerRevealError("root plan is missing a numeric Total Cost") from exc
            if not math.isfinite(optimizer_cost):
                raise OptimizerRevealError("optimizer cost is not finite")
            return OptimizerEvaluation(
                optimizer_cost=optimizer_cost,
                used_indexes=_used_indexes(normalized),
                plan_hash=_sha256_json(normalized),
            )
        except BaseException as exc:
            primary_error = exc
            self._rollback()
            raise
        finally:
            try:
                self._reset_verified()
            except Exception as cleanup_exc:
                if primary_error is None:
                    raise OptimizerRevealError(
                        f"HypoPG cleanup verification failed: {cleanup_exc}"
                    ) from cleanup_exc
                raise OptimizerRevealError(
                    "optimizer request failed and HypoPG cleanup verification also failed: "
                    f"request={primary_error}; cleanup={cleanup_exc}"
                ) from cleanup_exc


def _result_from_stored(response: StoredOptimizerResponse) -> RevealResult:
    return RevealResult(
        optimizer_cost=response.optimizer_cost,
        used_indexes=response.used_indexes,
        plan_hash=response.plan_hash,
        epoch_hash=response.epoch_hash,
    )


class _RevealService:
    """Internal epoch-bound reveal/cache/accounting service.

    It is constructed only by the mandatory run context.  The experiment-facing
    API is the module-level :func:`reveal`, which delegates to that context.
    """

    def __init__(
        self,
        *,
        store: CostStore,
        session: Optional[_OptimizerSession],
        relevant_relations: Optional[Iterable[object]],
        max_new_optimizer_calls: Optional[int] = None,
        allow_collection: bool = True,
    ) -> None:
        if max_new_optimizer_calls is not None and (
            isinstance(max_new_optimizer_calls, bool)
            or not isinstance(max_new_optimizer_calls, int)
            or max_new_optimizer_calls < 0
        ):
            raise ValueError(
                "max_new_optimizer_calls must be a finite non-negative integer"
            )
        if allow_collection and session is None:
            raise ValueError("a session-bound optimizer is required when collection is enabled")
        self.store = store
        self._session = session
        self._relevant_relations = (
            tuple(relevant_relations) if relevant_relations is not None else None
        )
        self.max_new_optimizer_calls = (
            max_new_optimizer_calls
        )
        self.allow_collection = bool(allow_collection)
        self._occurrence_bindings: dict[str, tuple[str, Optional[str]]] = {}
        self._critical_section = threading.RLock()

    @property
    def charged_optimizer_calls(self) -> int:
        """Compatibility alias for physical optimizer calls."""

        return self.store.physical_optimizer_calls

    @property
    def physical_optimizer_calls(self) -> int:
        return self.store.physical_optimizer_calls

    def _current_epoch(self) -> str:
        if self._session is None:
            return self.store.epoch_hash
        payload = self._session._capture_epoch(self._relevant_relations)
        current = str(payload.get("epoch_hash", "")).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", current):
            raise RevealError(
                "session epoch capture returned an invalid epoch hash"
            )
        if current != self.store.epoch_hash:
            raise EpochMismatchError(
                bound_epoch=self.store.epoch_hash,
                current_epoch=current,
            )
        return current

    def _budget_available(self) -> bool:
        return (
            self.max_new_optimizer_calls is None
            or self.store.physical_optimizer_calls < self.max_new_optimizer_calls
        )

    def _reveal(
        self,
        query: QuerySpec,
        configuration: ConfigurationSpec,
        *,
        _uncached: bool = False,
    ) -> RevealResult:
        """Return one exact response, charging only an uncached optimizer call."""
        with self._critical_section:
            return self._reveal_locked(query, configuration, _uncached=_uncached)

    def _lookup_ground_truth(
        self,
        query: QuerySpec,
        configuration: ConfigurationSpec,
    ) -> StoredOptimizerResponse:
        """Read exact evaluator ground truth without creating policy evidence."""

        with self._critical_section:
            epoch = self._current_epoch()
            response = self.store.require(
                query.occurrence_id,
                configuration.configuration_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
            )
            if response.epoch_hash != epoch:
                raise MissingOptimizerResponseError(
                    "stored response is outside the active optimizer epoch"
                )
            return response

    def _reveal_locked(
        self,
        query: QuerySpec,
        configuration: ConfigurationSpec,
        *,
        _uncached: bool,
    ) -> RevealResult:
        occurrence_id = query.occurrence_id
        configuration_id = configuration.configuration_id
        binding = (query.exact_sql_hash, query.template_id)
        previous_binding = self._occurrence_bindings.get(occurrence_id)
        if previous_binding is not None and previous_binding != binding:
            raise RevealError(
                f"occurrence_id {occurrence_id!r} was rebound within one service"
            )
        self._occurrence_bindings[occurrence_id] = binding
        try:
            epoch = self._current_epoch()
        except EpochMismatchError as exc:
            self.store.append_event(
                occurrence_id=occurrence_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
                configuration_id=configuration_id,
                optimizer_cost=None,
                physical_optimizer_call=0,
                ground_truth_hit=0,
                epoch_hash=exc.current_epoch,
                status=STATUS_EPOCH_MISMATCH,
            )
            raise
        except Exception:
            self.store.append_event(
                occurrence_id=occurrence_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
                configuration_id=configuration_id,
                optimizer_cost=None,
                physical_optimizer_call=0,
                ground_truth_hit=0,
                epoch_hash=self.store.epoch_hash,
                status=STATUS_EPOCH_UNVERIFIABLE,
            )
            raise

        if not _uncached:
            cached = self.store.lookup(
                occurrence_id,
                configuration_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
            )
            if cached is not None:
                if self.allow_collection:
                    cached = self.store.record_ground_truth_hit(
                        cached,
                        occurrence_id=occurrence_id,
                        template_id=query.template_id,
                    )
                return _result_from_stored(cached)

        if not self.allow_collection:
            self.store.append_event(
                occurrence_id=occurrence_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
                configuration_id=configuration_id,
                optimizer_cost=None,
                physical_optimizer_call=0,
                ground_truth_hit=0,
                epoch_hash=epoch,
                status=STATUS_MISSING_REJECTED,
            )
            raise MissingOptimizerResponseError(
                f"response is absent and collection is disabled: exact_sql_hash={query.exact_sql_hash}, "
                f"configuration={configuration_id}, epoch={epoch}"
            )

        if not self._budget_available():
            self.store.append_event(
                occurrence_id=occurrence_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
                configuration_id=configuration_id,
                optimizer_cost=None,
                physical_optimizer_call=0,
                ground_truth_hit=0,
                epoch_hash=epoch,
                status=STATUS_BUDGET_REJECTED,
            )
            raise OptimizerCallBudgetExceeded(
                f"max_new_optimizer_calls={self.max_new_optimizer_calls} exhausted"
            )

        assert self._session is not None
        try:
            raw_evaluation = self._session._evaluate(query, configuration)
            evaluation = OptimizerEvaluation(
                optimizer_cost=raw_evaluation.optimizer_cost,
                used_indexes=raw_evaluation.used_indexes,
                plan_hash=raw_evaluation.plan_hash,
            )
        except Exception as exc:
            self.store.append_event(
                occurrence_id=occurrence_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
                configuration_id=configuration_id,
                optimizer_cost=None,
                physical_optimizer_call=1,
                ground_truth_hit=0,
                epoch_hash=epoch,
                status=STATUS_OPTIMIZER_ERROR,
            )
            raise OptimizerRevealError(
                f"optimizer reveal failed for occurrence={occurrence_id}, configuration={configuration_id}: {exc}"
            ) from exc

        try:
            current_after = self._current_epoch()
        except EpochMismatchError as exc:
            self.store.append_event(
                occurrence_id=occurrence_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
                configuration_id=configuration_id,
                optimizer_cost=None,
                physical_optimizer_call=1,
                ground_truth_hit=0,
                epoch_hash=exc.current_epoch,
                status=STATUS_EPOCH_MISMATCH,
            )
            raise
        except Exception:
            self.store.append_event(
                occurrence_id=occurrence_id,
                exact_sql_hash=query.exact_sql_hash,
                template_id=query.template_id,
                configuration_id=configuration_id,
                optimizer_cost=None,
                physical_optimizer_call=1,
                ground_truth_hit=0,
                epoch_hash=self.store.epoch_hash,
                status=STATUS_EPOCH_UNVERIFIABLE,
            )
            raise

        persisted = self.store.append_event(
            occurrence_id=occurrence_id,
            exact_sql_hash=query.exact_sql_hash,
            template_id=query.template_id,
            configuration_id=configuration_id,
            optimizer_cost=evaluation.optimizer_cost,
            used_indexes=evaluation.used_indexes,
            plan_hash=evaluation.plan_hash,
            physical_optimizer_call=1,
            ground_truth_hit=0,
            epoch_hash=current_after,
            status=STATUS_OK,
        )
        assert persisted is not None
        return _result_from_stored(persisted)

    @staticmethod
    def _write_report(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)

    def _validate_optimizer_determinism(
        self,
        query: QuerySpec,
        configuration: ConfigurationSpec,
        *,
        report_path: Path | str,
    ) -> Tuple[RevealResult, RevealResult, RevealResult]:
        """Run the same request three uncached times and fail closed on drift."""
        results: list[RevealResult] = []
        error = ""
        try:
            for _ in range(3):
                results.append(self._reveal(query, configuration, _uncached=True))
        except Exception as exc:
            if (
                isinstance(exc, ConflictingStoredResponseError)
                and exc.response is not None
            ):
                results.append(_result_from_stored(exc.response))
            error = f"{type(exc).__name__}: {exc}"

        costs = [result.optimizer_cost for result in results]
        hashes = [result.plan_hash for result in results]
        passed = (
            not error
            and len(results) == 3
            and costs[0] == costs[1] == costs[2]
            and hashes[0] == hashes[1] == hashes[2]
        )
        if passed:
            try:
                self.store.lookup(
                    query.occurrence_id,
                    configuration.configuration_id,
                    exact_sql_hash=query.exact_sql_hash,
                    template_id=query.template_id,
                )
            except ConflictingStoredResponseError as exc:
                passed = False
                error = f"{type(exc).__name__}: {exc}"

        outcome = "PASS" if passed else "FAIL"
        lines = [
            "# Optimizer Determinism Report",
            "",
            f"- status: `{outcome}`",
            f"- occurrence_id: `{query.occurrence_id}`",
            f"- exact_sql_hash: `{query.exact_sql_hash}`",
            f"- template_id: `{query.template_id or ''}`",
            f"- configuration_id: `{configuration.configuration_id}`",
            f"- epoch_hash: `{self.store.epoch_hash}`",
            f"- required_repetitions: `3`",
            f"- completed_repetitions: `{len(results)}`",
            f"- optimizer_costs: `{json.dumps(costs, separators=(',', ':'))}`",
            f"- plan_hashes: `{json.dumps(hashes, separators=(',', ':'))}`",
        ]
        if error:
            lines.append(f"- error: `{error.replace('`', chr(39))}`")
        lines.extend(
            [
                "",
                "Collection may proceed only when status is PASS.",
                "",
            ]
        )
        self._write_report(Path(report_path), "\n".join(lines))
        if not passed:
            raise OptimizerNonDeterminismError(
                "same (query, configuration) did not produce three identical optimizer costs and plan hashes"
            )
        return results[0], results[1], results[2]
class _RunContext(Protocol):
    def _public_reveal(
        self, query: QuerySpec, configuration: ConfigurationSpec
    ) -> RevealResult:
        """Context-bound implementation for the sole public cost operation."""


def reveal(
    context: _RunContext,
    query: QuerySpec,
    configuration: ConfigurationSpec,
) -> RevealResult:
    """Return optimizer cost only through a validated mandatory run context."""
    if not hasattr(context, "_public_reveal"):
        raise TypeError("reveal() requires a validated EvaluationRunContext")
    return context._public_reveal(query, configuration)
