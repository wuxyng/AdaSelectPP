import ast
import inspect

import pytest

import tools.pr20d_real_exec_prefix_swap as pr20d


PREFIX = ("movie_info", ("mi_movie_id",))
COMPOSITE = ("movie_info", ("mi_movie_id", "mi_info_type_id"))
OTHER = ("cast_info", ("ci_movie_id",))


def test_prefix_swap_physical_config_is_atomic():
    baseline = {PREFIX, OTHER}

    swapped, feasible, reason = pr20d.build_prefix_swap_config(
        baseline,
        PREFIX,
        COMPOSITE,
        max_num=2,
    )

    assert feasible
    assert reason == ""
    assert swapped == {COMPOSITE, OTHER}
    assert PREFIX not in swapped
    assert len(swapped) == len(baseline)


def test_generated_ddl_does_not_exceed_max_num():
    ddls = pr20d.generate_index_ddls(
        {PREFIX, OTHER},
        run_label="unit",
        round_id=3,
        config_label="baseline",
        max_num=2,
    )

    assert len(ddls) == 2
    assert all(ddl.create_sql.startswith('CREATE INDEX "pr20d_') for ddl in ddls)
    assert all("DROP INDEX IF EXISTS" in ddl.drop_sql for ddl in ddls)

    with pytest.raises(ValueError, match="exceeds max_num"):
        pr20d.generate_index_ddls(
            {PREFIX, COMPOSITE, OTHER},
            run_label="unit",
            round_id=3,
            config_label="swap",
            max_num=2,
        )


def test_experiment_refuses_without_explicit_physical_flag():
    with pytest.raises(PermissionError, match="experimental indexes"):
        pr20d.ensure_experimental_allowed(
            experimental_physical_indexes=False,
            database="job",
        )

    with pytest.raises(PermissionError, match="database"):
        pr20d.ensure_experimental_allowed(
            experimental_physical_indexes=True,
            database="",
        )

    pr20d.ensure_experimental_allowed(
        experimental_physical_indexes=True,
        database="job_scratch",
    )


def test_pr20d_tool_does_not_import_online_policy_or_candidate_generation():
    source = inspect.getsource(pr20d)
    tree = ast.parse(source)
    banned_modules = {
        "adasel.ada_select",
        "adaselect_pp.candidate_gen_v2",
        "adaselect_pp.candidate_gen_v2.generator",
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
    assert "AdaSelect" not in referenced_names
    assert "MCIGCandidateGenerator" not in referenced_names
    assert "candidate_generator" not in referenced_names

