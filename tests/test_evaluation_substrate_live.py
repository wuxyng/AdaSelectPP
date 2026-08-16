"""Opt-in live PostgreSQL/HypoPG verification for Evaluation Substrate v0."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from tools.evaluation_substrate import (
    ConfigurationSpec,
    EvaluationRunContext,
    IndexDefinition,
    QuerySpec,
    reveal,
)
from tools.evaluation_substrate.epoch_fingerprint import collect_epoch_fingerprint
from tools.evaluation_substrate.manifest import build_manifest, write_manifest_atomic


SCRATCH_DSN = os.environ.get("EVALUATION_SUBSTRATE_TEST_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN,
    reason="EVALUATION_SUBSTRATE_TEST_DSN is not an explicitly authorized scratch DSN",
)


def _connect():
    try:
        import psycopg

        return psycopg.connect(SCRATCH_DSN)
    except ImportError:
        psycopg2 = pytest.importorskip("psycopg2")
        return psycopg2.connect(SCRATCH_DSN)


def test_live_hypopg_same_session_and_epoch_drift(tmp_path):
    connection = _connect()
    table = "evaluation_substrate_live_" + uuid.uuid4().hex[:12]
    query = QuerySpec("live-q", f"SELECT * FROM {table} WHERE a = 42")
    configuration = ConfigurationSpec((IndexDefinition(table, ("a",)),))
    original_random_page_cost = None
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE TABLE public."{table}" (a integer NOT NULL, b text)')
            cursor.execute(
            f'INSERT INTO public."{table}" SELECT g, md5(g::text) FROM generate_series(1, 20000) g'
            )
            cursor.execute(f'ANALYZE public."{table}"')
            cursor.execute("SHOW random_page_cost")
            original_random_page_cost = str(cursor.fetchone()[0])
        connection.commit()

        scope = [("public", table)]
        epoch = collect_epoch_fingerprint(connection, scope)
        workload = tmp_path / "workload.sql"
        candidates = tmp_path / "candidate_snapshot_tier1.csv"
        workload.write_text(query.sql + "\n", encoding="utf-8")
        candidates.write_text(
            "candidate_id,table,columns,source,generator_version,snapshot_hash\n"
            f"live-1,{table},a,live-scratch,manual-v1,{'4' * 64}\n",
            encoding="utf-8",
        )
        manifest = build_manifest(
            run_id="live",
            epoch=epoch,
            workload_file=workload,
            candidate_snapshot_tier1=candidates,
            repo_root=tmp_path,
            created_at=datetime.now(timezone.utc),
            collection_tier="tier1",
            tier1_queries=[query],
            tier1_configurations=[configuration],
            workload_relation_scope_complete=True,
            _test_code_state={
                "git_commit": "0" * 40,
                "git_dirty": False,
                "git_dirty_status": "CLEAN",
                "git_status_porcelain": [],
            },
        )
        run_dir = tmp_path / "run"
        write_manifest_atomic(run_dir, manifest)
        with EvaluationRunContext._open_collection_for_test(
            run_directory=run_dir,
            repo_root=tmp_path,
            connection=connection,
        ) as context:
            authorization = context.validate_determinism(query, configuration)
            result = reveal(context, query, configuration)
            assert authorization.charged_measurements == 3
            assert result.optimizer_cost >= 0
            assert result.used_indexes == (f"hypopg:{table}(a)",)
            assert len(result.plan_hash) == 64

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('random_page_cost', %s, false)",
                (str(float(original_random_page_cost) + 0.25),),
            )
        connection.commit()
        changed = collect_epoch_fingerprint(connection, scope)
        assert changed["epoch_hash"] != epoch["epoch_hash"]
    finally:
        try:
            if original_random_page_cost is not None:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('random_page_cost', %s, false)",
                        (original_random_page_cost,),
                    )
                    cursor.execute(f'DROP TABLE IF EXISTS public."{table}"')
                connection.commit()
        finally:
            connection.close()
