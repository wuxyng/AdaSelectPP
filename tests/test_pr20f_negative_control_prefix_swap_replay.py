import ast
import csv
import inspect
import re
import sys
import types
from pathlib import Path

import pytest

import tools.pr20f_negative_control_prefix_swap_replay as pr20f


PREFIX = ("movie_info", ("mi_movie_id",))
COMPOSITE = ("movie_info", ("mi_movie_id", "mi_info_type_id"))
OTHER = ("cast_info", ("ci_movie_id",))
ALT_WIDTH2 = "cast_info(ci_movie_id,ci_role_id)"
TARGET = "movie_info(mi_movie_id,mi_info_type_id)"


class FakePhysicalDB:
    def __init__(
        self,
        *,
        existing=(),
        drop_failures=(),
        leave_after_drop=(),
        catalog_fail_calls=(),
        close_error=None,
    ):
        self.existing = set(existing)
        self.drop_failures = set(drop_failures)
        self.leave_after_drop = set(leave_after_drop)
        self.catalog_fail_calls = set(catalog_fail_calls)
        self.close_error = close_error
        self.catalog_calls = 0
        self.events = []

    def exec_fetchall_params(self, query, params):
        self.catalog_calls += 1
        names = tuple(params[0])
        self.events.append(("catalog", names))
        assert "c.relname = ANY(%s)" in query
        assert "relkind" not in query
        if self.catalog_calls in self.catalog_fail_calls:
            raise RuntimeError("catalog unavailable")
        return [(name,) for name in sorted(self.existing.intersection(names))]

    def execute_only(self, query):
        match = re.fullmatch(r'DROP INDEX IF EXISTS "([^"]+)"', query)
        assert match, query
        name = match.group(1)
        self.events.append(("drop", name))
        if name in self.drop_failures:
            raise RuntimeError(f"drop failed for {name}")
        if name not in self.leave_after_drop:
            self.existing.discard(name)

    def rollback(self):
        self.events.append(("rollback",))

    def close(self):
        self.events.append(("close",))
        if self.close_error is not None:
            raise self.close_error


def _ddl(name: str, index=PREFIX):
    return pr20f.IndexDDL(
        index=index,
        name=name,
        create_sql=f'CREATE INDEX "{name}" ON "movie_info" ("mi_movie_id")',
        drop_sql=f'DROP INDEX IF EXISTS "{name}"',
    )


def _main_args(tmp_path: Path):
    return [
        "--database",
        "job_scratch",
        "--metrics-csv",
        str(tmp_path / "metrics.csv"),
        "--pr20c-rounds-csv",
        str(tmp_path / "rounds.csv"),
        "--pr20c-candidates-csv",
        str(tmp_path / "candidates.csv"),
        "--output-root",
        str(tmp_path / "out"),
        "--experimental-physical-indexes",
    ]


def _patch_main_dependencies(monkeypatch, db, run_experiment):
    connector_module = types.ModuleType("database.database_connector")
    connector_module.DatabaseConnector = lambda *args, **kwargs: db
    monkeypatch.setitem(sys.modules, "database.database_connector", connector_module)
    monkeypatch.setattr(pr20f, "load_workloads", lambda *args, **kwargs: [["SELECT 1"]])
    monkeypatch.setattr(pr20f, "run_experiment", run_experiment)


def _paths(tmp_path: Path):
    return tuple(tmp_path / f"artifact_{i}.csv" for i in range(5))


def _write_metrics(tmp_path: Path) -> Path:
    path = tmp_path / "metrics.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["round", "old", "new"])
        writer.writeheader()
        old_config = "[(\"movie_info\", (\"mi_movie_id\",)), (\"cast_info\", (\"ci_movie_id\",))]"
        new_config = "[(\"movie_info\", (\"mi_movie_id\", \"mi_info_type_id\")), (\"cast_info\", (\"ci_movie_id\",))]"
        for rid in [1, 2, 3, 4, 22]:
            writer.writerow({"round": rid, "old": old_config, "new": new_config})
    return path


def _write_pr20c_inputs(tmp_path: Path):
    rounds_path = tmp_path / "rounds.csv"
    candidates_path = tmp_path / "candidates.csv"
    rows = [
        # Positive-arm census: target is best and passes. PR20f excludes it by default.
        (1, TARGET, 0.09, 0.09, 1),
        # Positive target signal, but a different width-2 candidate is best.
        (2, ALT_WIDTH2, 0.07, 0.04, 1),
        # Small positive target signal below the default margin.
        (3, TARGET, 0.002, 0.002, 0),
        # Non-positive target signal.
        (4, ALT_WIDTH2, 0.01, -0.01, 0),
        # PR20e warning reference.
        (22, TARGET, 0.0262, 0.0262, 1),
    ]
    with rounds_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["round_id", "best_swap_index", "best_swap_relative_improvement"])
        writer.writeheader()
        for rid, best_index, best_rel, _target_rel, _pass in rows:
            writer.writerow({
                "round_id": rid,
                "best_swap_index": best_index,
                "best_swap_relative_improvement": best_rel,
            })
    with candidates_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["round_id", "width2_index", "swap_relative_improvement", "oracle_pass_swap"],
        )
        writer.writeheader()
        for rid, _best_index, _best_rel, target_rel, oracle_pass in rows:
            writer.writerow({
                "round_id": rid,
                "width2_index": TARGET,
                "swap_relative_improvement": target_rel,
                "oracle_pass_swap": oracle_pass,
            })
    return rounds_path, candidates_path


def test_selection_covers_negative_control_categories_and_excludes_positive_arm(tmp_path):
    metrics = _write_metrics(tmp_path)
    rounds, candidates = _write_pr20c_inputs(tmp_path)

    selected = pr20f.select_negative_control_rounds(
        rounds_csv=rounds,
        candidates_csv=candidates,
        metrics_csv=metrics,
        prefix_index=PREFIX,
        composite_index=COMPOSITE,
        max_num=10,
    )

    by_round = {item.round_id: item.sample_category for item in selected}
    assert 1 not in by_round
    assert by_round[2] == "non_target_best_positive"
    assert by_round[3] == "predicted_flat_or_low"
    assert by_round[4] == "predicted_negative"
    assert by_round[22] == "near_margin"


def test_read_executed_config_uses_old_not_new(tmp_path):
    metrics = _write_metrics(tmp_path)

    configs = pr20f.read_executed_configs(metrics)

    assert PREFIX in configs[1]
    assert COMPOSITE not in configs[1]


def test_baseline_swap_configs_differ_only_by_prefix_to_composite():
    swapped, feasible, reason = pr20f.build_prefix_swap_config(
        {PREFIX, OTHER},
        PREFIX,
        COMPOSITE,
        max_num=2,
    )

    assert feasible
    assert reason == ""
    assert swapped == {COMPOSITE, OTHER}


def test_physical_execution_refuses_without_experimental_flag():
    with pytest.raises(PermissionError):
        pr20f.ensure_experimental_allowed(
            experimental_physical_indexes=False,
            database="job",
        )


def test_unstable_rounds_are_emitted_and_excluded_from_primary_aggregates():
    stable = {
        "round_id": 1,
        "sample_category": "predicted_flat_or_low",
        "unstable_excluded": 0,
        "real_exec_rel_improvement": 0.02,
        "real_outcome": "improved",
        "gate_threshold": 0.03,
    }
    unstable = {
        "round_id": 2,
        "sample_category": "excluded_unstable",
        "unstable_excluded": 1,
        "real_exec_rel_improvement": 0.50,
        "real_outcome": "excluded_unstable",
        "gate_threshold": 0.03,
    }
    excluded = [{"round_id": 2, "unstable_reason": "swap_cv_high"}]

    summary = pr20f.summarize_category_metrics([stable, unstable], excluded)
    flat_row = next(row for row in summary if row["sample_category"] == "predicted_flat_or_low")
    excluded_row = next(row for row in summary if row["row_type"] == "excluded_unstable")

    assert flat_row["round_count"] == 1
    assert flat_row["improved_count"] == 1
    assert excluded_row["excluded_round_count"] == 1
    assert excluded_row["excluded_round_ids"] == "2"


def test_gate_threshold_simulation_produces_all_gate_outcomes():
    rows = []
    for rid, rel, real_outcome in [
        (1, 0.04, "improved"),
        (2, 0.04, "flat"),
        (3, 0.02, "flat"),
        (4, 0.02, "improved"),
    ]:
        rows.extend(pr20f.expand_gate_rows(
            {
                "round_id": rid,
                "sample_category": "predicted_flat_or_low",
                "unstable_excluded": 0,
                "target_swap_whatif_rel_improvement": rel,
                "real_outcome": real_outcome,
            },
            gate_thresholds=[0.03],
        ))

    assert {row["gate_outcome"] for row in rows} == {
        "true_accept",
        "false_accept",
        "true_reject",
        "false_reject",
    }


def test_gate_metrics_are_computed_per_threshold():
    rows = []
    for rel, real_outcome in [(0.04, "improved"), (0.04, "flat"), (0.02, "flat"), (0.02, "improved")]:
        rows.extend(pr20f.expand_gate_rows(
            {
                "round_id": len(rows),
                "unstable_excluded": 0,
                "target_swap_whatif_rel_improvement": rel,
                "real_outcome": real_outcome,
            },
            gate_thresholds=[0.03, 0.05],
        ))

    metrics = pr20f.summarize_gate_metrics(rows)
    by_threshold = {row["threshold"]: row for row in metrics}

    assert set(by_threshold) == {0.03, 0.05}
    assert by_threshold[0.03]["tested_count"] == 4
    assert by_threshold[0.03]["accept_count"] == 2
    assert by_threshold[0.03]["true_accept_count"] == 1
    assert by_threshold[0.03]["false_accept_count"] == 1
    assert by_threshold[0.03]["true_reject_count"] == 1
    assert by_threshold[0.03]["false_reject_count"] == 1


def test_docs_and_output_labels_avoid_model_fit_claim_language():
    doc = Path("docs/pr20f_negative_control_prefix_swap_replay.md").read_text(encoding="utf-8").lower()
    output_terms = " ".join(pr20f.ROUND_COLUMNS + pr20f.SUMMARY_COLUMNS + pr20f.GATE_METRICS_COLUMNS).lower()
    banned = ["calibration", "bias", "underestimation", "overestimation"]

    for word in banned:
        assert word not in doc
        assert word not in output_terms


def test_pr20f_tool_does_not_import_online_policy_or_candidate_generation():
    source = inspect.getsource(pr20f)
    tree = ast.parse(source)
    banned_modules = {
        "adasel.ada_select",
        "adaselect_pp.candidate_gen_v2",
        "adaselect_pp.candidate_gen_v2.generator",
        "adaselect_pp.selector",
        "adaselect_pp.materialization",
    }
    imported_modules = set()
    referenced_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
        elif isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)

    assert not (imported_modules & banned_modules)
    assert "_choose_config" not in source
    assert "MCIGCandidateGenerator" not in referenced_names
    assert "candidate_generator" not in referenced_names


def test_main_normal_success_strict_cleanup_happens_before_close(monkeypatch, tmp_path, capsys):
    name = "pr20f_round_1_baseline"
    ddl = _ddl(name)
    db = FakePhysicalDB()

    def fake_run(**kwargs):
        pr20f.register_run_owned_indexes(db, kwargs["run_owned_indexes"], [ddl])
        db.existing.add(name)
        db.events.append(("created", name))
        return _paths(tmp_path)

    _patch_main_dependencies(monkeypatch, db, fake_run)

    assert pr20f.main(_main_args(tmp_path)) == 0
    drop_at = db.events.index(("drop", name))
    verify_at = max(i for i, event in enumerate(db.events) if event[0] == "catalog")
    close_at = db.events.index(("close",))
    assert drop_at < verify_at < close_at
    assert name not in db.existing
    assert capsys.readouterr().out.splitlines() == [str(path) for path in _paths(tmp_path)]


def test_final_registry_covers_more_than_one_round(monkeypatch, tmp_path):
    first = _ddl("pr20f_round_1_baseline")
    second = _ddl("pr20f_round_2_swap", index=COMPOSITE)
    db = FakePhysicalDB()

    def fake_run(**kwargs):
        registry = kwargs["run_owned_indexes"]
        pr20f.register_run_owned_indexes(db, registry, [first])
        db.existing.add(first.name)
        db.existing.discard(first.name)  # Existing per-round cleanup already succeeded.
        pr20f.register_run_owned_indexes(db, registry, [second])
        db.existing.add(second.name)
        return _paths(tmp_path)

    _patch_main_dependencies(monkeypatch, db, fake_run)

    assert pr20f.main(_main_args(tmp_path)) == 0
    dropped = [event[1] for event in db.events if event[0] == "drop"]
    assert set(dropped) == {first.name, second.name}


@pytest.mark.parametrize("failure", [ValueError("experiment boom"), KeyboardInterrupt()])
def test_experiment_failure_after_creation_still_cleans_before_close(
    monkeypatch, tmp_path, failure
):
    ddl = _ddl("pr20f_exception_round")
    db = FakePhysicalDB()

    def fake_run(**kwargs):
        pr20f.register_run_owned_indexes(db, kwargs["run_owned_indexes"], [ddl])
        db.existing.add(ddl.name)
        raise failure

    _patch_main_dependencies(monkeypatch, db, fake_run)

    with pytest.raises(type(failure)):
        pr20f.main(_main_args(tmp_path))
    assert ddl.name not in db.existing
    assert db.events.index(("drop", ddl.name)) < db.events.index(("close",))


def test_strict_cleanup_continues_after_one_drop_failure():
    first = _ddl("pr20f_first")
    second = _ddl("pr20f_second", index=COMPOSITE)
    registry = {first.name: first, second.name: second}
    db = FakePhysicalDB(
        existing=registry,
        drop_failures={second.name},
    )

    with pytest.raises(pr20f.PR20fStrictCleanupError) as excinfo:
        pr20f.strict_cleanup_run_owned_indexes(db, registry)

    dropped = [event[1] for event in db.events if event[0] == "drop"]
    assert dropped == [second.name, first.name]
    assert ("rollback",) in db.events
    assert second.name in str(excinfo.value)
    assert first.name not in db.existing


def test_cleanup_failure_prevents_successful_main_return(monkeypatch, tmp_path):
    ddl = _ddl("pr20f_cleanup_failure")
    db = FakePhysicalDB(drop_failures={ddl.name})

    def fake_run(**kwargs):
        pr20f.register_run_owned_indexes(db, kwargs["run_owned_indexes"], [ddl])
        db.existing.add(ddl.name)
        return _paths(tmp_path)

    _patch_main_dependencies(monkeypatch, db, fake_run)

    with pytest.raises(pr20f.PR20fStrictCleanupError):
        pr20f.main(_main_args(tmp_path))


def test_leftover_exact_index_after_drop_prevents_success(monkeypatch, tmp_path):
    ddl = _ddl("pr20f_leftover")
    db = FakePhysicalDB(leave_after_drop={ddl.name})

    def fake_run(**kwargs):
        pr20f.register_run_owned_indexes(db, kwargs["run_owned_indexes"], [ddl])
        db.existing.add(ddl.name)
        return _paths(tmp_path)

    _patch_main_dependencies(monkeypatch, db, fake_run)

    with pytest.raises(pr20f.PR20fStrictCleanupError, match="remaining exact index"):
        pr20f.main(_main_args(tmp_path))


def test_preexisting_exact_target_collision_fails_closed_without_deleting_it():
    ddl = _ddl("pr20f_preexisting")
    unrelated = "pr20f_unrelated"
    db = FakePhysicalDB(existing={ddl.name, unrelated})
    registry = {}

    with pytest.raises(pr20f.PR20fIndexCollisionError, match=ddl.name):
        pr20f.register_run_owned_indexes(db, registry, [ddl])

    assert registry == {}
    assert db.existing == {ddl.name, unrelated}
    assert not any(event[0] == "drop" for event in db.events)


def test_evaluate_round_checks_collision_before_first_physical_mutation(monkeypatch):
    target_name = pr20f.pr20f_index_name(
        run_label="unit",
        round_id=7,
        config_label="baseline",
        index=PREFIX,
    )
    db = FakePhysicalDB(existing={target_name})
    mutations = []
    monkeypatch.setattr(
        pr20f,
        "drop_config_indexes",
        lambda *args, **kwargs: mutations.append("drop"),
    )
    monkeypatch.setattr(
        pr20f,
        "materialize_config",
        lambda *args, **kwargs: mutations.append("create"),
    )

    with pytest.raises(pr20f.PR20fIndexCollisionError, match=target_name):
        pr20f.evaluate_round(
            db=db,
            selected_round=pr20f.SelectedRound(
                round_id=7,
                sample_category="predicted_flat_or_low",
                target_swap_whatif_rel_improvement=0.0,
                best_swap_index=TARGET,
                best_swap_whatif_rel_improvement=0.0,
                is_target_best=True,
            ),
            workload=[],
            baseline_config={PREFIX, OTHER},
            prefix_index=PREFIX,
            composite_index=COMPOSITE,
            max_num=2,
            warmup=1,
            repeats=3,
            max_cv=0.2,
            outcome_threshold=0.01,
            gate_thresholds=[0.01],
            run_label="unit",
            run_order_id="alternating_pairs",
            run_owned_indexes={},
        )

    assert mutations == []


def test_run_experiment_threads_one_registry_across_rounds(monkeypatch, tmp_path):
    selected = [
        pr20f.SelectedRound(i, "predicted_flat_or_low", 0.0, TARGET, 0.0, True)
        for i in (0, 1)
    ]
    monkeypatch.setattr(
        pr20f,
        "read_executed_configs",
        lambda path: {0: {PREFIX}, 1: {PREFIX}},
    )
    monkeypatch.setattr(
        pr20f,
        "select_negative_control_rounds",
        lambda **kwargs: selected,
    )
    registry_ids = []

    def fake_evaluate_round(**kwargs):
        registry = kwargs["run_owned_indexes"]
        registry_ids.append(id(registry))
        ddl = _ddl(f"pr20f_round_{kwargs['selected_round'].round_id}")
        registry[ddl.name] = ddl
        return [], [], None

    monkeypatch.setattr(pr20f, "evaluate_round", fake_evaluate_round)
    monkeypatch.setattr(pr20f, "write_csv", lambda path, columns, rows: path)
    registry = {}

    pr20f.run_experiment(
        db=FakePhysicalDB(),
        workloads=[[], []],
        metrics_csv=tmp_path / "metrics.csv",
        pr20c_rounds_csv=tmp_path / "rounds.csv",
        pr20c_candidates_csv=tmp_path / "candidates.csv",
        output_root=tmp_path / "out",
        prefix_index=PREFIX,
        composite_index=COMPOSITE,
        max_num=2,
        selection_mode="all",
        max_rounds_per_category=0,
        positive_anchor_count=0,
        gate_thresholds=[0.01],
        gate_margin_threshold=0.03,
        near_margin_band=0.005,
        warmup=1,
        repeats=3,
        max_cv=0.2,
        outcome_threshold=0.01,
        run_label="unit",
        run_order_id="alternating_pairs",
        run_owned_indexes=registry,
    )

    assert len(set(registry_ids)) == 1
    assert set(registry) == {"pr20f_round_0", "pr20f_round_1"}


def test_strict_cleanup_is_idempotent_when_per_round_cleanup_already_removed_index():
    ddl = _ddl("pr20f_already_removed")
    registry = {ddl.name: ddl}
    db = FakePhysicalDB()

    pr20f.strict_cleanup_run_owned_indexes(db, registry)
    pr20f.strict_cleanup_run_owned_indexes(db, registry)

    assert [event for event in db.events if event[0] == "drop"] == [
        ("drop", ddl.name),
        ("drop", ddl.name),
    ]


def test_strict_cleanup_targets_only_exact_registered_names():
    owned = _ddl("pr20f_owned_exact")
    unrelated = "pr20f_similar_but_unregistered"
    db = FakePhysicalDB(existing={owned.name, unrelated})

    pr20f.strict_cleanup_run_owned_indexes(db, {owned.name: owned})

    assert owned.name not in db.existing
    assert unrelated in db.existing
    assert ("drop", unrelated) not in db.events
    catalog_targets = [set(event[1]) for event in db.events if event[0] == "catalog"]
    assert catalog_targets == [{owned.name}]


def test_database_close_failure_is_not_swallowed(monkeypatch, tmp_path, capsys):
    db = FakePhysicalDB(close_error=RuntimeError("close boom"))
    _patch_main_dependencies(monkeypatch, db, lambda **kwargs: _paths(tmp_path))

    with pytest.raises(RuntimeError, match="close boom"):
        pr20f.main(_main_args(tmp_path))
    assert capsys.readouterr().out == ""


def test_no_successful_paths_are_printed_after_cleanup_failure(monkeypatch, tmp_path, capsys):
    ddl = _ddl("pr20f_no_success_print")
    db = FakePhysicalDB(drop_failures={ddl.name})

    def fake_run(**kwargs):
        pr20f.register_run_owned_indexes(db, kwargs["run_owned_indexes"], [ddl])
        db.existing.add(ddl.name)
        return _paths(tmp_path)

    _patch_main_dependencies(monkeypatch, db, fake_run)

    with pytest.raises(pr20f.PR20fStrictCleanupError):
        pr20f.main(_main_args(tmp_path))
    assert capsys.readouterr().out == ""


def test_existing_successful_main_behavior_is_unchanged_when_cleanup_succeeds(
    monkeypatch, tmp_path, capsys
):
    db = FakePhysicalDB()
    paths = _paths(tmp_path)
    _patch_main_dependencies(monkeypatch, db, lambda **kwargs: paths)

    assert pr20f.main(_main_args(tmp_path)) == 0
    assert capsys.readouterr().out.splitlines() == [str(path) for path in paths]
    assert db.events[-1] == ("close",)


def test_cleanup_verification_failure_is_observable():
    ddl = _ddl("pr20f_verify_failure")
    db = FakePhysicalDB(existing={ddl.name}, catalog_fail_calls={1})

    with pytest.raises(pr20f.PR20fStrictCleanupError, match="verification"):
        pr20f.strict_cleanup_run_owned_indexes(db, {ddl.name: ddl})


def test_experiment_and_cleanup_failures_are_both_reported(monkeypatch, tmp_path):
    ddl = _ddl("pr20f_double_failure")
    db = FakePhysicalDB(drop_failures={ddl.name})

    def fake_run(**kwargs):
        pr20f.register_run_owned_indexes(db, kwargs["run_owned_indexes"], [ddl])
        db.existing.add(ddl.name)
        raise ValueError("experiment boom")

    _patch_main_dependencies(monkeypatch, db, fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        pr20f.main(_main_args(tmp_path))
    assert "ValueError: experiment boom" in str(excinfo.value)
    assert "PR20fStrictCleanupError" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValueError)
