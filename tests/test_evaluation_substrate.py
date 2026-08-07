import ast
import csv
import inspect
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.evaluation_substrate.cost_store import (
    STATUS_BUDGET_REJECTED,
    STATUS_EPOCH_MISMATCH,
    ConflictingStoredResponseError,
    CostStore,
    CostStoreError,
    MissingOptimizerResponseError,
)
import tools.evaluation_substrate as public_api
from tools.evaluation_substrate.epoch_fingerprint import (
    EpochFingerprintError,
    OPTIONAL_PLANNER_GUCS,
    PLANNER_GUCS,
    canonical_sha256,
    collect_epoch_fingerprint,
    compute_epoch_hash,
)
from tools.evaluation_substrate.evidence import (
    EVIDENCE_COLUMNS,
    EVIDENCE_MISSING_REJECTED,
    EVIDENCE_OK,
    EvidenceStoreError,
    ExactResponseKey,
    OCCURRENCE_COLUMNS,
)
from tools.evaluation_substrate.manifest import (
    ManifestError,
    build_manifest,
    build_tier1_inventory,
    write_manifest_atomic,
)
from tools.evaluation_substrate.reveal import (
    DeterminismValidationRequired,
    EpochMismatchError,
    OptimizerCallBudgetExceeded,
    OptimizerEvaluation,
    OptimizerNonDeterminismError,
    OptimizerRevealError,
    RevealError,
    _HypoPGSession,
    _RevealService,
)
from tools.evaluation_substrate.run_context import (
    ArtifactDriftError,
    DeterminismAuthorizationError,
    EvaluationRunContext,
    RunContextError,
    WriterLockError,
)
from tools.evaluation_substrate.schema import (
    CandidateSnapshot,
    CandidateSnapshotError,
    CandidateSnapshotRow,
    CandidateTier,
    ConfigurationSpec,
    IndexDefinition,
    MetricsLineageError,
    QuerySpec,
    load_tier1_candidate_snapshot,
    parse_metrics_configuration,
)


EPOCH_A = "a" * 64
EPOCH_B = "b" * 64
PLAN_STABLE = "1" * 64
PLAN_A = "2" * 64
PLAN_B = "3" * 64


def query() -> QuerySpec:
    return QuerySpec("q-000", "SELECT * FROM t WHERE a = 1")


def configuration() -> ConfigurationSpec:
    return ConfigurationSpec((IndexDefinition("t", ("a",)),))


class FakeOptimizer:
    def __init__(self, evaluations=None, epochs=None):
        self.calls = 0
        self.evaluations = list(evaluations or [])

        self.connection = self
        self.epoch = EPOCH_A
        self.epochs = iter(epochs) if epochs is not None else None

    def _capture_epoch(self, _relevant_relations):
        return {"epoch_hash": next(self.epochs) if self.epochs is not None else self.epoch}

    def _evaluate(self, _query, _configuration):
        self.calls += 1
        if self.evaluations:
            return self.evaluations.pop(0)
        return OptimizerEvaluation(12.5, ("hypopg:t(a)",), PLAN_STABLE)


def make_service(
    store: CostStore,
    backend: FakeOptimizer | None,
    *,
    max_new_optimizer_calls=None,
    allow_collection=True,
):
    return _RevealService(
        store=store,
        session=backend,
        relevant_relations=None,
        max_new_optimizer_calls=max_new_optimizer_calls,
        allow_collection=allow_collection,
    )


def read_response_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_core_has_no_online_adaselectpp_imports():
    package = Path(__file__).resolve().parents[1] / "tools" / "evaluation_substrate"
    forbidden_roots = {"adasel", "adaselect_pp", "database", "util"}
    violations = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split(".", 1)[0] in forbidden_roots:
                    violations.append(f"{path.name}:{node.lineno}:{name}")
    assert violations == []


def test_t1_same_query_configuration_three_times_is_identical(tmp_path):
    backend = FakeOptimizer()
    store = CostStore(tmp_path / "optimizer_responses.csv", epoch_hash=EPOCH_A)
    service = make_service(store, backend, max_new_optimizer_calls=3)

    results = service._validate_optimizer_determinism(
        query(), configuration(), report_path=tmp_path / "determinism_report.md"
    )

    assert len(results) == 3
    assert {result.optimizer_cost for result in results} == {12.5}
    assert {result.plan_hash for result in results} == {PLAN_STABLE}
    assert backend.calls == 3
    assert service.physical_optimizer_calls == 3
    assert "status: `PASS`" in (tmp_path / "determinism_report.md").read_text(
        encoding="utf-8"
    )


def test_determinism_mismatch_fails_closed_and_writes_report(tmp_path):
    backend = FakeOptimizer(
        [
            OptimizerEvaluation(10.0, (), PLAN_A),
            OptimizerEvaluation(10.0, (), PLAN_A),
            OptimizerEvaluation(10.1, (), PLAN_B),
        ]
    )
    service = make_service(
        CostStore(tmp_path / "optimizer_responses.csv", epoch_hash=EPOCH_A),
        backend,
        max_new_optimizer_calls=3,
    )

    with pytest.raises(OptimizerNonDeterminismError):
        service._validate_optimizer_determinism(
            query(), configuration(), report_path=tmp_path / "determinism_report.md"
        )

    report = (tmp_path / "determinism_report.md").read_text(encoding="utf-8")
    assert "status: `FAIL`" in report
    assert "10.1" in report


def test_t2_ground_truth_hit_does_not_increase_physical_calls(tmp_path):
    backend = FakeOptimizer()
    path = tmp_path / "optimizer_responses.csv"
    service = make_service(CostStore(path, epoch_hash=EPOCH_A), backend)

    first = service._reveal(query(), configuration())
    calls_after_miss = service.physical_optimizer_calls
    second = service._reveal(query(), configuration())

    assert first == second
    assert backend.calls == 1
    assert calls_after_miss == service.physical_optimizer_calls == 1
    rows = read_response_rows(path)
    assert [
        (row["physical_optimizer_call"], row["ground_truth_hit"])
        for row in rows
    ] == [
        ("1", "0"),
        ("0", "1"),
    ]
    assert {row["exact_sql_hash"] for row in rows} == {query().exact_sql_hash}


def test_occurrence_id_cannot_be_rebound_after_store_reopen(tmp_path):
    path = tmp_path / "optimizer_responses.csv"
    collecting = make_service(CostStore(path, epoch_hash=EPOCH_A), FakeOptimizer())
    collecting._reveal(query(), configuration())

    backend = FakeOptimizer()
    reopened = make_service(CostStore(path, epoch_hash=EPOCH_A), backend)
    changed = QuerySpec(query().occurrence_id, "SELECT * FROM t WHERE a = 2")
    with pytest.raises(CostStoreError, match="conflicting exact SQL"):
        reopened._reveal(changed, configuration())
    assert backend.calls == 0


def test_force_refresh_contradiction_is_persisted_but_never_returned(tmp_path):
    backend = FakeOptimizer(
        [
            OptimizerEvaluation(10.0, (), PLAN_A),
            OptimizerEvaluation(11.0, (), PLAN_B),
        ]
    )
    service = make_service(
        CostStore(tmp_path / "optimizer_responses.csv", epoch_hash=EPOCH_A), backend
    )
    service._reveal(query(), configuration())

    with pytest.raises(ConflictingStoredResponseError):
        service._reveal(query(), configuration(), _uncached=True)

    assert backend.calls == 2
    assert service.physical_optimizer_calls == 2


def test_t3_missing_response_is_rejected_not_approximated(tmp_path):
    path = tmp_path / "optimizer_responses.csv"
    service = make_service(
        CostStore(path, epoch_hash=EPOCH_A), None, allow_collection=False
    )

    with pytest.raises(MissingOptimizerResponseError):
        service._lookup_ground_truth(query(), configuration())

    rows = read_response_rows(path)
    assert rows == []


def test_t4_different_epoch_invalidates_stored_response(tmp_path):
    path = tmp_path / "optimizer_responses.csv"
    collecting = make_service(CostStore(path, epoch_hash=EPOCH_A), FakeOptimizer())
    collecting._reveal(query(), configuration())

    new_epoch_store = CostStore(path, epoch_hash=EPOCH_B)
    assert new_epoch_store.stale_epoch_rows == 1
    assert new_epoch_store.lookup(
        query().occurrence_id,
        configuration().configuration_id,
        exact_sql_hash=query().exact_sql_hash,
        template_id=query().template_id,
    ) is None
    replay = make_service(new_epoch_store, None, allow_collection=False)
    with pytest.raises(MissingOptimizerResponseError):
        replay._reveal(query(), configuration())


def test_epoch_drift_rejects_even_a_cached_response_and_persists_event(tmp_path):
    path = tmp_path / "optimizer_responses.csv"
    store = CostStore(path, epoch_hash=EPOCH_A)
    first = make_service(store, FakeOptimizer())
    first._reveal(query(), configuration())
    drifted_backend = FakeOptimizer()
    drifted_backend.epoch = EPOCH_B
    drifted = make_service(store, drifted_backend)

    with pytest.raises(EpochMismatchError):
        drifted._reveal(query(), configuration())

    assert read_response_rows(path)[-1]["status"] == STATUS_EPOCH_MISMATCH


def test_epoch_drift_after_optimizer_call_is_still_charged(tmp_path):
    backend = FakeOptimizer(epochs=(EPOCH_A, EPOCH_B))
    service = make_service(
        CostStore(tmp_path / "optimizer_responses.csv", epoch_hash=EPOCH_A), backend
    )

    with pytest.raises(EpochMismatchError):
        service._reveal(query(), configuration())

    assert backend.calls == 1
    assert service.physical_optimizer_calls == 1


def test_t5_configuration_serialization_is_deterministic():
    a = IndexDefinition("T", ("A",))
    bc = IndexDefinition("t", ("b", "c"))
    left = ConfigurationSpec((bc, a))
    right = ConfigurationSpec((a, bc))

    assert left.indexes == right.indexes
    assert left.canonical_json == right.canonical_json
    assert left.configuration_id == right.configuration_id
    assert left.canonical_text == "t(a);t(b,c)"


def test_t6_old_and_new_metrics_fields_cannot_silently_enter_substrate():
    old_value = "[('t', ('a',))]"
    parsed = parse_metrics_configuration({"old": old_value, "new": "[]"}, field="old")
    assert parsed == configuration()

    with pytest.raises(MetricsLineageError, match="post-window recommendation"):
        parse_metrics_configuration({"old": old_value, "new": "[]"}, field="new")
    with pytest.raises(MetricsLineageError, match="missing executed field 'old'"):
        parse_metrics_configuration({"new": old_value}, field="old")
    with pytest.raises(TypeError):
        parse_metrics_configuration({"old": old_value})
    with pytest.raises(MetricsLineageError, match="explicit literal"):
        parse_metrics_configuration({"old": ""}, field="old")


def test_tier1_candidate_snapshot_requires_exact_explicit_schema(tmp_path):
    path = tmp_path / "candidate_snapshot_tier1.csv"
    path.write_text(
        "candidate_id,table,columns,source,generator_version,snapshot_hash\n"
        f"c1,T,A,recorded_trace,probe_grow_fair@8b0e355,{'1' * 64}\n",
        encoding="utf-8",
    )
    snapshot = load_tier1_candidate_snapshot(path)
    assert snapshot.rows[0].index_definition == IndexDefinition("t", ("a",))

    bad = tmp_path / "bad.csv"
    bad.write_text("candidate_id,table\nc1,t\n", encoding="utf-8")
    with pytest.raises(CandidateSnapshotError, match="expected exact header"):
        load_tier1_candidate_snapshot(bad)


def test_budget_guard_rejects_before_call_and_persists_event(tmp_path):
    path = tmp_path / "optimizer_responses.csv"
    backend = FakeOptimizer()
    service = make_service(
        CostStore(path, epoch_hash=EPOCH_A),
        backend,
        max_new_optimizer_calls=0,
    )

    with pytest.raises(OptimizerCallBudgetExceeded):
        service._reveal(query(), configuration())

    assert backend.calls == 0
    assert read_response_rows(path)[0]["status"] == STATUS_BUDGET_REJECTED


@pytest.mark.parametrize("invalid_guard", [True, 1.5, "1"])
def test_budget_guard_requires_an_exact_integer(tmp_path, invalid_guard):
    with pytest.raises(ValueError, match="finite non-negative integer"):
        make_service(
            CostStore(tmp_path / "optimizer_responses.csv", epoch_hash=EPOCH_A),
            FakeOptimizer(),
            max_new_optimizer_calls=invalid_guard,
        )


def test_collection_requires_session_bound_optimizer(tmp_path):
    with pytest.raises(ValueError, match="session-bound optimizer"):
        make_service(
            CostStore(tmp_path / "optimizer_responses.csv", epoch_hash=EPOCH_A),
            None,
        )


class FakeEpochCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, statement):
        if statement.startswith("BEGIN TRANSACTION"):
            self.connection.snapshot_begins += 1
            self.rows = []
        elif "SELECT version()" in statement:
            self.rows = [
                (
                    "PostgreSQL 16.2", "160002", "job", "42", "lx", "lx",
                    '"$user", public', "on", "C", "C", "c", None, None,
                )
            ]
        elif "to_regprocedure('pg_catalog.pg_database_collation_actual_version" in statement:
            self.rows = [("pg_database_collation_actual_version(oid)",)]
        elif "pg_database_collation_actual_version(oid)" in statement:
            self.rows = [("2.36",)]
        elif "FROM pg_extension" in statement:
            self.rows = [("1.4.1",)]
        elif "name LIKE 'enable" in statement:
            self.rows = sorted(self.connection.enable_gucs.items())
        elif "FROM pg_settings" in statement:
            self.rows = sorted(self.connection.gucs.items())
        elif "FROM pg_class c JOIN pg_namespace" in statement:
            if "pg_get_viewdef" in statement:
                self.rows = [("public", "t", "r", None, None, None, None, None, None)]
            elif "relallvisible" in statement:
                self.rows = [
                    ("public", "t", "r", "10", "1000", self.connection.relallvisible, None)
                ]
            elif "relispartition" in statement:
                self.rows = list(self.connection.partitions)
            else:
                raise AssertionError(statement)
        elif "FROM pg_stats" in statement:
            self.rows = [
                (
                    "public",
                    "t",
                    "a",
                    "false",
                    "0",
                    "4",
                    "-1",
                    None,
                    None,
                    "{1,1000}",
                    "1",
                    None,
                    None,
                    None,
                )
            ]
        elif "FROM pg_attribute" in statement:
            self.rows = [
                ("public", "t", "1", "a", "23", "integer", "-1", "0", "", "true", "false", "", "")
            ]
        elif "FROM pg_constraint" in statement:
            self.rows = list(self.connection.constraints)
        elif "FROM pg_inherits" in statement:
            self.rows = list(self.connection.inheritance)
        elif "FROM pg_statistic_ext x" in statement and "JOIN pg_statistic_ext_data" in statement:
            self.rows = list(self.connection.extended_data)
        elif "FROM pg_statistic_ext x" in statement:
            self.rows = list(self.connection.extended_definitions)
        elif "FROM pg_index" in statement:
            self.rows = [
                (
                    "public",
                    "t",
                    "t_pkey",
                    "true",
                    "true",
                    "true",
                    "true",
                    "true",
                    "3",
                    "1000",
                    None,
                    None,
                    "CREATE UNIQUE INDEX t_pkey ON public.t USING btree (a)",
                )
            ]
        else:  # pragma: no cover - exposes unexpected new epoch queries
            raise AssertionError(statement)

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class FakeEpochConnection:
    def __init__(self):
        self.gucs = {
            name: f"value-{name}"
            for name in PLANNER_GUCS + OPTIONAL_PLANNER_GUCS
        }
        self.enable_gucs = {"enable_hashjoin": "on", "enable_seqscan": "on"}
        self.relallvisible = "5"
        self.constraints = [
            ("public", "t", "t_pkey", "p", "true", "false", "false", "false", "PRIMARY KEY (a)")
        ]
        self.partitions = []
        self.inheritance = []
        self.extended_definitions = [
            ("public", "t", "t_stats", "CREATE STATISTICS t_stats ON a FROM t", "-1")
        ]
        self.extended_data = [
            ("public", "t", "t_stats", '{"1": 1000}', None, None, None)
        ]
        self.snapshot_begins = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeEpochCursor(self)

    def rollback(self):
        self.rollbacks += 1


def test_epoch_fingerprint_and_manifest_bind_required_inputs(tmp_path):
    connection = FakeEpochConnection()
    epoch = collect_epoch_fingerprint(connection, ["public.t"])
    assert epoch["epoch_hash"] == compute_epoch_hash(epoch)
    assert set(epoch["database_environment"]["planner_gucs"]) == set(PLANNER_GUCS)
    assert epoch["statistics_fingerprint"]["pg_stats"]["row_count"] == 1
    assert connection.snapshot_begins == 1
    assert connection.rollbacks >= 2

    workload = tmp_path / "workload.txt"
    candidates = tmp_path / "candidate_snapshot_tier1.csv"
    metrics = tmp_path / "metrics.csv"
    trace = tmp_path / "trace.csv"
    workload.write_text("SELECT 1\t0\n", encoding="utf-8")
    candidates.write_text(
        "candidate_id,table,columns,source,generator_version,snapshot_hash\n"
        f"c1,t,a,test,frozen-v1,{'4' * 64}\n",
        encoding="utf-8",
    )
    metrics.write_text("round,old,new\n0,[],[]\n", encoding="utf-8")
    trace.write_text("round,table,cols\n", encoding="utf-8")
    manifest = build_manifest(
        run_id="sample",
        epoch=epoch,
        workload_file=workload,
        candidate_snapshot_tier1=candidates,
        metrics_file=metrics,
        trace_file=trace,
        repo_root=tmp_path,
        _test_code_state={
            "git_commit": "0" * 40,
            "git_dirty": False,
            "git_dirty_status": "CLEAN",
            "git_status_porcelain": [],
        },
        created_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        collection_tier="tier1",
        tier1_queries=[query()],
        tier1_configurations=[configuration()],
        workload_relation_scope_complete=True,
    )
    manifest_path = write_manifest_atomic(tmp_path / "run", manifest)
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["input_artifacts"]["workload"]["sha256"]
    assert persisted["input_artifacts"]["metrics"]["sha256"]
    assert persisted["input_artifacts"]["trace"]["sha256"]
    assert persisted["epoch_hash"] == epoch["epoch_hash"]


def test_epoch_fingerprint_rejects_missing_required_planner_guc():
    epoch = collect_epoch_fingerprint(FakeEpochConnection(), ["public.t"])
    del epoch["database_environment"]["planner_gucs"]["work_mem"]

    with pytest.raises(EpochFingerprintError, match="exactly the required"):
        compute_epoch_hash(epoch)


class FakeHypoPGCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params=None):
        if statement == "SELECT hypopg_reset()":
            self.connection.oids.clear()
            self.row = (True,)
            self.rows = []
        elif "FROM hypopg_list_indexes" in statement:
            self.rows = [
                (oid, "CREATE INDEX ON public.t USING btree (a)")
                for oid in sorted(self.connection.oids)
            ]
            self.row = self.rows[0] if self.rows else None
        elif statement == "SELECT * FROM hypopg_create_index(%s)":
            assert params and params[0].startswith("CREATE INDEX ON")
            oid = "100"
            name = "<100>btree_t_a"
            self.connection.oids.add(oid)
            self.row = (oid, name)
            self.rows = [self.row]
        elif statement.startswith("EXPLAIN (FORMAT JSON)"):
            self.connection.explain_calls = getattr(self.connection, "explain_calls", 0) + 1
            self.row = (
                [
                    {
                        "Plan": {
                            "Node Type": "Index Scan",
                            "Index Name": "<100>btree_t_a",
                            "Relation Name": "t",
                            "Total Cost": 12.5,
                        }
                    }
                ],
            )
            self.rows = [self.row]
        else:  # pragma: no cover - exposes unexpected optimizer SQL
            raise AssertionError(statement)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class FakeHypoPGConnection:
    def __init__(self):
        self.oids = set()
        self.explain_calls = 0

    def cursor(self):
        return FakeHypoPGCursor(self)

    def rollback(self):
        pass

    def commit(self):
        pass


class FakePostgresCursor(FakeHypoPGCursor):
    def execute(self, statement, params=None):
        try:
            return super().execute(statement, params)
        except AssertionError:
            epoch_cursor = FakeEpochCursor(self.connection)
            epoch_cursor.execute(statement)
            self.rows = list(epoch_cursor.rows)
            self.row = self.rows[0] if self.rows else None
            return None


class FakePostgresConnection(FakeEpochConnection):
    def __init__(self):
        super().__init__()
        self.oids = set()
        self.explain_calls = 0

    def cursor(self):
        return FakePostgresCursor(self)

    def commit(self):
        pass


def create_test_run(
    tmp_path,
    *,
    tier=CandidateTier.TIER1,
    queries=None,
    configurations=None,
    max_new_optimizer_calls=100,
    connection=None,
    candidate_column="a",
    run_id="sample",
):
    connection = connection or FakePostgresConnection()
    queries = list(queries or [query()])
    configurations = list(configurations or [ConfigurationSpec(), configuration()])
    workload = tmp_path / "workload.txt"
    candidates = tmp_path / f"candidate_snapshot_{tier.value}.csv"
    workload.write_text("\n".join(item.sql for item in queries) + "\n", encoding="utf-8")
    candidates.write_text(
        "candidate_id,table,columns,source,generator_version,snapshot_hash\n"
        f"c1,t,{candidate_column},test,frozen-v1,{'4' * 64}\n",
        encoding="utf-8",
    )
    epoch = collect_epoch_fingerprint(
        connection,
        ["public.t"] if tier == CandidateTier.TIER1 else None,
    )
    kwargs = {
        "run_id": run_id,
        "epoch": epoch,
        "workload_file": workload,
        "repo_root": tmp_path,
        "created_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
        "collection_tier": tier,
        "workload_relation_scope_complete": True,
        "_test_code_state": {
            "git_commit": "0" * 40,
            "git_dirty": False,
            "git_dirty_status": "CLEAN",
            "git_status_porcelain": [],
        },
    }
    if tier == CandidateTier.TIER1:
        kwargs.update(
            candidate_snapshot_tier1=candidates,
            tier1_queries=queries,
            tier1_configurations=configurations,
        )
    else:
        kwargs.update(
            candidate_snapshot_tier2=candidates,
            max_new_optimizer_calls=max_new_optimizer_calls,
        )
    manifest = build_manifest(**kwargs)
    run_dir = tmp_path / "run"
    write_manifest_atomic(run_dir, manifest)
    return run_dir, connection, workload, candidates, manifest


def open_test_context(run_dir, connection, repo_root):
    return EvaluationRunContext._open_collection_for_test(
        run_directory=run_dir,
        repo_root=repo_root,
        connection=connection,
    )


def test_hypopg_backend_returns_only_cost_indexes_and_canonical_plan_hash(tmp_path):
    run_dir, connection, *_ = create_test_run(tmp_path)
    with open_test_context(run_dir, connection, tmp_path) as context:
        authorization = context.validate_determinism(query(), configuration())
        result = public_api.reveal(context, query(), configuration())
        again = public_api.reveal(context, query(), configuration())

        assert authorization.charged_measurements == 3
        assert result == again
        assert result.optimizer_cost == 12.5
        assert result.used_indexes == ("hypopg:t(a)",)
        assert len(result.plan_hash) == 64


def test_hypopg_backend_rejects_authoritative_configuration_mismatch(tmp_path):
    mismatched = ConfigurationSpec((IndexDefinition("t", ("b",)),))
    run_dir, connection, *_ = create_test_run(
        tmp_path, configurations=[mismatched], candidate_column="b"
    )
    with open_test_context(run_dir, connection, tmp_path) as context:
        with pytest.raises(OptimizerNonDeterminismError):
            context.validate_determinism(query(), mismatched)
        assert "definitions differ" in (run_dir / "determinism_report.md").read_text(
            encoding="utf-8"
        )


def test_hypopg_backend_rejects_data_modifying_cte_before_database_access(tmp_path):
    modifying = QuerySpec(
        "q-write",
        "WITH changed AS (DELETE FROM t RETURNING *) SELECT * FROM changed",
    )
    run_dir, connection, *_ = create_test_run(
        tmp_path, queries=[modifying], configurations=[ConfigurationSpec()]
    )
    with open_test_context(run_dir, connection, tmp_path) as context:
        with pytest.raises(OptimizerNonDeterminismError):
            context.validate_determinism(modifying, ConfigurationSpec())
        assert "read-only" in (run_dir / "determinism_report.md").read_text(
            encoding="utf-8"
        )
        assert connection.oids == set()


def test_tier2_requires_finite_new_call_guard(tmp_path):
    with pytest.raises(ManifestError, match="finite non-negative integer"):
        create_test_run(
            tmp_path,
            tier=CandidateTier.TIER2,
            max_new_optimizer_calls=None,
        )


def candidate_snapshot(tier: CandidateTier) -> CandidateSnapshot:
    return CandidateSnapshot(
        tier,
        (
            CandidateSnapshotRow(
                candidate_id="c1",
                table="t",
                columns=("a",),
                source="test-fixture",
                generator_version="frozen-v1",
                snapshot_hash="4" * 64,
            ),
        ),
    )


def test_tier1_requires_explicit_candidate_universe_and_collects_supplied_product(tmp_path):
    configurations = [ConfigurationSpec(), configuration()]
    run_dir, connection, *_ = create_test_run(
        tmp_path, configurations=configurations
    )
    with open_test_context(run_dir, connection, tmp_path) as context:
        context.validate_determinism(query(), configuration())
        summary = context.collect_tier1([query()], configurations)

        assert summary.requested_responses == 2
        assert summary.physical_optimizer_calls == 1
        assert summary.evaluator_ground_truth_hits == 1
        assert summary.charged_policy_probes == 0


def test_tier1_rejects_duplicate_occurrence_ids_before_collection(tmp_path):
    connection = FakePostgresConnection()
    with pytest.raises(ManifestError, match="unique occurrence_id"):
        create_test_run(
            tmp_path,
            connection=connection,
            queries=[query(), query()],
            configurations=[configuration()],
        )
    assert connection.explain_calls == 0


def test_configuration_outside_candidate_universe_is_rejected(tmp_path):
    outside = ConfigurationSpec((IndexDefinition("t", ("b",)),))
    run_dir, connection, *_ = create_test_run(
        tmp_path,
        tier=CandidateTier.TIER2,
        max_new_optimizer_calls=10,
    )
    with open_test_context(run_dir, connection, tmp_path) as context:
        context.validate_determinism(query(), configuration())
        calls_after_gate = connection.explain_calls
        with pytest.raises(RevealError, match="outside the manifest-bound candidate universe"):
            context.collect_tier2(iter([(query(), outside)]))
        assert connection.explain_calls == calls_after_gate


def test_public_package_has_no_direct_optimizer_evaluator():
    forbidden = {"HypoPGOptimizer", "RevealService", "CostStore"}
    assert forbidden.isdisjoint(public_api.__all__)
    assert all(not name.startswith("HypoPG") for name in public_api.__all__)
    assert not hasattr(public_api, "HypoPGOptimizer")


def test_internal_backend_is_not_a_public_bypass():
    import tools.evaluation_substrate.reveal as reveal_module

    assert not hasattr(reveal_module, "HypoPGOptimizer")
    assert "_HypoPGSession" not in getattr(reveal_module, "__all__", ())
    assert "reveal" in public_api.__all__


def test_public_reveal_has_no_force_refresh_escape_hatch():
    assert "force_refresh" not in inspect.signature(public_api.reveal).parameters
    open_parameters = inspect.signature(EvaluationRunContext.open_collection).parameters
    assert "_allow_test_manifest" not in open_parameters
    assert "_epoch_connection" not in open_parameters


def test_public_reveal_requires_validated_run_context():
    with pytest.raises(TypeError, match="EvaluationRunContext"):
        public_api.reveal(FakeOptimizer(), query(), configuration())


@pytest.mark.parametrize("artifact_role", ["workload", "candidate"])
def test_manifest_bound_artifact_drift_is_rejected(tmp_path, artifact_role):
    run_dir, connection, workload, candidates, _manifest = create_test_run(tmp_path)
    target = workload if artifact_role == "workload" else candidates
    target.write_text(target.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    with pytest.raises(ArtifactDriftError, match="drift"):
        open_test_context(run_dir, connection, tmp_path)


def test_artifact_drift_after_context_open_is_rejected_before_collection(tmp_path):
    run_dir, connection, workload, *_ = create_test_run(tmp_path)
    with open_test_context(run_dir, connection, tmp_path) as context:
        workload.write_text(workload.read_text(encoding="utf-8") + "-- drift\n", encoding="utf-8")
        with pytest.raises(ArtifactDriftError, match="drift"):
            context.validate_determinism(query(), configuration())


def test_tier1_inventory_mismatch_is_rejected(tmp_path):
    configurations = [ConfigurationSpec(), configuration()]
    run_dir, connection, *_ = create_test_run(
        tmp_path, configurations=configurations
    )
    with open_test_context(run_dir, connection, tmp_path) as context:
        context.validate_determinism(query(), configuration())
        with pytest.raises(RunContextError, match="inventory differs"):
            context.collect_tier1([query()], [configuration()])


def test_mutating_boolean_cannot_authorize_collection(tmp_path):
    run_dir, connection, *_ = create_test_run(tmp_path)
    with open_test_context(run_dir, connection, tmp_path) as context:
        with pytest.raises(AttributeError):
            context._determinism_validated = True
        with pytest.raises(DeterminismValidationRequired):
            public_api.reveal(context, query(), configuration())


def test_determinism_authorization_cannot_cross_manifest_or_run(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_run, first_connection, *_ = create_test_run(
        first_root, run_id="first-run"
    )
    with open_test_context(first_run, first_connection, first_root) as first:
        first.validate_determinism(query(), configuration())
    second_run, second_connection, *_ = create_test_run(
        second_root, run_id="second-run"
    )
    for name in ("determinism_gate.json", "determinism_report.md"):
        (second_run / name).write_bytes((first_run / name).read_bytes())
    with open_test_context(second_run, second_connection, second_root) as second:
        with pytest.raises(DeterminismAuthorizationError, match="does not match"):
            public_api.reveal(second, query(), configuration())


@pytest.mark.parametrize(
    "field,replacement",
    [("epoch_hash", EPOCH_B), ("candidate_snapshot_sha256", "f" * 64)],
)
def test_determinism_authorization_cannot_cross_epoch_or_snapshot(
    tmp_path, field, replacement
):
    run_dir, connection, *_ = create_test_run(tmp_path)
    with open_test_context(run_dir, connection, tmp_path) as context:
        context.validate_determinism(query(), configuration())
        gate_path = run_dir / "determinism_gate.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate[field] = replacement
        payload = dict(gate)
        payload.pop("gate_payload_sha256")
        gate["gate_payload_sha256"] = canonical_sha256(payload)
        gate_path.write_text(json.dumps(gate), encoding="utf-8")
        with pytest.raises(DeterminismAuthorizationError, match="does not match"):
            public_api.reveal(context, query(), configuration())


def test_optimizer_and_epoch_connections_cannot_differ(tmp_path):
    run_dir, connection, *_ = create_test_run(tmp_path)
    with pytest.raises(RunContextError, match="same connection"):
        EvaluationRunContext._open_collection_for_test(
            run_directory=run_dir,
            repo_root=tmp_path,
            connection=connection,
            epoch_connection=FakePostgresConnection(),
        )


def test_expanded_planner_guc_changes_epoch_hash():
    connection = FakeEpochConnection()
    before = collect_epoch_fingerprint(connection, ["public.t"])
    connection.gucs["seq_page_cost"] = "999"
    after = collect_epoch_fingerprint(connection, ["public.t"])
    assert before["epoch_hash"] != after["epoch_hash"]


def test_relallvisible_changes_epoch_hash():
    connection = FakeEpochConnection()
    before = collect_epoch_fingerprint(connection, ["public.t"])
    connection.relallvisible = "6"
    after = collect_epoch_fingerprint(connection, ["public.t"])
    assert before["epoch_hash"] != after["epoch_hash"]


def test_extended_statistics_data_changes_epoch_hash():
    connection = FakeEpochConnection()
    before = collect_epoch_fingerprint(connection, ["public.t"])
    connection.extended_data = [
        ("public", "t", "t_stats", '{"1": 999}', None, None, None)
    ]
    after = collect_epoch_fingerprint(connection, ["public.t"])
    assert before["epoch_hash"] != after["epoch_hash"]


@pytest.mark.parametrize("component", ["constraint", "partition"])
def test_constraints_and_partitions_change_epoch_hash(component):
    connection = FakeEpochConnection()
    before = collect_epoch_fingerprint(connection, ["public.t"])
    if component == "constraint":
        connection.constraints.append(
            ("public", "t", "t_positive", "c", "true", "false", "false", "false", "CHECK (a > 0)")
        )
    else:
        connection.partitions.append(("public", "t", "RANGE (a)", "FOR VALUES FROM (0) TO (10)"))
    after = collect_epoch_fingerprint(connection, ["public.t"])
    assert before["epoch_hash"] != after["epoch_hash"]


def test_second_response_store_writer_is_rejected(tmp_path):
    run_dir, connection, *_ = create_test_run(tmp_path)
    first = open_test_context(run_dir, connection, tmp_path)
    try:
        with pytest.raises(WriterLockError, match="already exists"):
            open_test_context(run_dir, connection, tmp_path)
    finally:
        first.close()


def test_final_budget_unit_cannot_be_spent_twice_in_process(tmp_path):
    run_dir, connection, *_ = create_test_run(
        tmp_path,
        tier=CandidateTier.TIER2,
        max_new_optimizer_calls=4,
    )
    with open_test_context(run_dir, connection, tmp_path) as context:
        context.validate_determinism(query(), configuration())
        barrier = threading.Barrier(3)
        outcomes = []

        def worker(item):
            barrier.wait()
            try:
                public_api.reveal(context, item, configuration())
                outcomes.append("OK")
            except Exception as exc:
                outcomes.append(type(exc).__name__)

        threads = [
            threading.Thread(target=worker, args=(QuerySpec("q-1", "SELECT 1 FROM t"),)),
            threading.Thread(target=worker, args=(QuerySpec("q-2", "SELECT 2 FROM t"),)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        assert sorted(outcomes) == ["OK", "OptimizerCallBudgetExceeded"]
        assert context.physical_optimizer_calls == 4
        assert context.charged_policy_probes == 0


def prepare_replay_run(
    tmp_path,
    queries,
    *,
    tier=CandidateTier.TIER1,
):
    requests = list(queries)
    run_dir, connection, *_ = create_test_run(
        tmp_path,
        tier=tier,
        queries=requests,
        configurations=[configuration()],
        max_new_optimizer_calls=100,
    )
    with open_test_context(run_dir, connection, tmp_path) as context:
        context.validate_determinism(requests[0], configuration())
        if tier == CandidateTier.TIER1:
            context.collect_tier1(requests, [configuration()])
    return run_dir, connection


def open_replay_context(
    run_dir,
    repo_root,
    session_id,
    *,
    seeded_evidence=(),
):
    return EvaluationRunContext._open_replay_for_test(
        run_directory=run_dir,
        repo_root=repo_root,
        evidence_session_id=session_id,
        seeded_evidence=seeded_evidence,
    )


def read_csv_rows(path):
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_qcp_same_template_different_literals_are_distinct_and_each_charged(tmp_path):
    first = QuerySpec("occ-1", "SELECT * FROM t WHERE a = 1", "template-a")
    second = QuerySpec("occ-2", "SELECT * FROM t WHERE a = 2", "template-a")
    assert first.exact_sql_hash != second.exact_sql_hash

    run_dir, _connection = prepare_replay_run(tmp_path, [first, second])
    with open_replay_context(run_dir, tmp_path, "session-literals") as replay:
        public_api.reveal(replay, first, configuration())
        public_api.reveal(replay, second, configuration())
        assert replay.charged_policy_probes == 2

    events = read_csv_rows(run_dir / "evidence_events.csv")
    assert [row["charged_policy_probe"] for row in events] == ["1", "1"]
    response_hashes = {
        row["exact_sql_hash"] for row in read_response_rows(run_dir / "optimizer_responses.csv")
    }
    assert response_hashes == {first.exact_sql_hash, second.exact_sql_hash}


def test_qcp_different_occurrences_same_exact_sql_share_response_and_evidence(tmp_path):
    sql = "SELECT * FROM t WHERE a = 7"
    first = QuerySpec("occ-a", sql, "template-a")
    second = QuerySpec("occ-b", sql, "template-a")
    run_dir, _connection = prepare_replay_run(tmp_path, [first, second])

    with open_replay_context(run_dir, tmp_path, "session-shared") as replay:
        first_result = public_api.reveal(replay, first, configuration())
        second_result = public_api.reveal(replay, second, configuration())
        assert first_result == second_result
        assert replay.charged_policy_probes == 1

    events = read_csv_rows(run_dir / "evidence_events.csv")
    assert [
        (row["occurrence_id"], row["charged_policy_probe"], row["evidence_hit"])
        for row in events
    ] == [("occ-a", "1", "0"), ("occ-b", "0", "1")]
    occurrences = read_csv_rows(run_dir / "workload_occurrences.csv")
    assert [row["occurrence_id"] for row in occurrences] == ["occ-a", "occ-b"]
    assert {row["exact_sql_hash"] for row in occurrences} == {first.exact_sql_hash}


def test_qcp_template_id_never_changes_exact_response_identity(tmp_path):
    sql = "SELECT * FROM t WHERE a = 9"
    first = QuerySpec("occ-template-a", sql, "template-a")
    second = QuerySpec("occ-template-b", sql, "template-b")
    first_key = ExactResponseKey(
        first.exact_sql_hash, configuration().configuration_id, EPOCH_A
    )
    second_key = ExactResponseKey(
        second.exact_sql_hash, configuration().configuration_id, EPOCH_A
    )
    assert first_key == second_key

    run_dir, _connection = prepare_replay_run(tmp_path, [first, second])
    with open_replay_context(run_dir, tmp_path, "session-templates") as replay:
        public_api.reveal(replay, first, configuration())
        public_api.reveal(replay, second, configuration())
        assert replay.charged_policy_probes == 1


def test_qcp_occurrence_rebinding_to_different_exact_sql_fails_closed(tmp_path):
    first = QuerySpec("stable-occurrence", "SELECT * FROM t WHERE a = 1")
    changed = QuerySpec("stable-occurrence", "SELECT * FROM t WHERE a = 2")
    run_dir, _connection = prepare_replay_run(
        tmp_path, [first], tier=CandidateTier.TIER2
    )

    with open_replay_context(run_dir, tmp_path, "session-rebind") as replay:
        public_api.reveal(replay, first, configuration())
        with pytest.raises(CostStoreError, match="occurrence_id.*conflicting"):
            public_api.reveal(replay, changed, configuration())
        assert replay.charged_policy_probes == 1


def test_qcp_preloaded_ground_truth_first_reveal_charged_second_free(tmp_path):
    item = QuerySpec("preloaded", "SELECT * FROM t WHERE a = 11")
    run_dir, _connection = prepare_replay_run(tmp_path, [item])
    physical_rows_before = read_response_rows(run_dir / "optimizer_responses.csv")

    with open_replay_context(run_dir, tmp_path, "new-policy-session") as replay:
        physical_calls = replay.physical_optimizer_calls
        public_api.reveal(replay, item, configuration())
        assert replay.charged_policy_probes == 1
        public_api.reveal(replay, item, configuration())
        assert replay.charged_policy_probes == 1
        assert replay.physical_optimizer_calls == physical_calls

    assert read_response_rows(run_dir / "optimizer_responses.csv") == physical_rows_before
    events = read_csv_rows(run_dir / "evidence_events.csv")
    assert [(row["charged_policy_probe"], row["evidence_hit"]) for row in events] == [
        ("1", "0"),
        ("0", "1"),
    ]


def test_qcp_sessions_are_isolated_and_explicit_seed_is_free(tmp_path):
    item = QuerySpec("shared-item", "SELECT * FROM t WHERE a = 13")
    run_dir, _connection = prepare_replay_run(tmp_path, [item])

    with open_replay_context(run_dir, tmp_path, "session-a") as first:
        public_api.reveal(first, item, configuration())
        assert first.charged_policy_probes == 1
    with open_replay_context(run_dir, tmp_path, "session-b") as second:
        public_api.reveal(second, item, configuration())
        assert second.charged_policy_probes == 1
    with open_replay_context(
        run_dir,
        tmp_path,
        "session-seeded",
        seeded_evidence=((item, configuration()),),
    ) as seeded:
        public_api.reveal(seeded, item, configuration())
        assert seeded.charged_policy_probes == 0


def test_qcp_same_exact_request_under_different_epoch_is_not_reusable(tmp_path):
    path = tmp_path / "optimizer_responses.csv"
    collecting = make_service(CostStore(path, epoch_hash=EPOCH_A), FakeOptimizer())
    collecting._reveal(query(), configuration())
    old_key = ExactResponseKey(
        query().exact_sql_hash, configuration().configuration_id, EPOCH_A
    )
    new_key = ExactResponseKey(
        query().exact_sql_hash, configuration().configuration_id, EPOCH_B
    )
    assert old_key != new_key
    assert CostStore(path, epoch_hash=EPOCH_B).lookup(
        query().occurrence_id,
        configuration().configuration_id,
        exact_sql_hash=query().exact_sql_hash,
    ) is None


def test_qcp_missing_ground_truth_is_charged_and_never_approximated(tmp_path):
    collected = QuerySpec("collected", "SELECT * FROM t WHERE a = 1")
    missing = QuerySpec("missing", "SELECT * FROM t WHERE a = 999")
    run_dir, connection = prepare_replay_run(
        tmp_path, [collected], tier=CandidateTier.TIER2
    )
    physical_rows_before = read_response_rows(run_dir / "optimizer_responses.csv")

    with open_replay_context(run_dir, tmp_path, "session-missing") as replay:
        with pytest.raises(MissingOptimizerResponseError, match="missing exact response"):
            public_api.reveal(replay, missing, configuration())
        assert replay.charged_policy_probes == 1
        assert connection.explain_calls == 3

    assert read_response_rows(run_dir / "optimizer_responses.csv") == physical_rows_before
    event = read_csv_rows(run_dir / "evidence_events.csv")[-1]
    assert event["status"] == EVIDENCE_MISSING_REJECTED
    assert event["charged_policy_probe"] == "1"
    assert event["evidence_hit"] == "0"


def test_qcp_identical_sql_occurrences_preserve_workload_multiplicity(tmp_path):
    sql = "SELECT * FROM t WHERE a = 21"
    occurrences = [QuerySpec(f"multiplicity-{index}", sql) for index in range(3)]
    inventory = build_tier1_inventory(occurrences, [configuration()])
    assert len(inventory["queries"]) == 3
    assert len({item["exact_sql_hash"] for item in inventory["queries"]}) == 1

    run_dir, _connection = prepare_replay_run(tmp_path, occurrences)
    with open_replay_context(run_dir, tmp_path, "session-multiplicity") as replay:
        for item in occurrences:
            public_api.reveal(replay, item, configuration())
        assert replay.charged_policy_probes == 1

    occurrence_rows = read_csv_rows(run_dir / "workload_occurrences.csv")
    assert len(occurrence_rows) == 3
    assert [row["occurrence_id"] for row in occurrence_rows] == [
        item.occurrence_id for item in occurrences
    ]


def test_qcp_template_estimate_cannot_enter_exact_response_key_or_store(tmp_path):
    assert tuple(inspect.signature(ExactResponseKey).parameters) == (
        "exact_sql_hash",
        "configuration_id",
        "epoch_hash",
    )
    with pytest.raises(TypeError):
        ExactResponseKey(
            query().exact_sql_hash,
            configuration().configuration_id,
            EPOCH_A,
            template_id="template-summary",
        )
    store = CostStore(tmp_path / "optimizer_responses.csv", epoch_hash=EPOCH_A)
    assert "template_estimate" not in store.path.read_text(encoding="utf-8")


def test_query_id_is_strict_occurrence_compatibility_alias():
    compatible = QuerySpec(query_id="legacy-q", sql="SELECT 1")
    assert compatible.occurrence_id == compatible.query_id == "legacy-q"
    assert compatible.exact_sql_hash == compatible.query_hash
    with pytest.raises(Exception, match="cannot both be supplied"):
        QuerySpec("new-id", "SELECT 1", query_id="legacy-id")
