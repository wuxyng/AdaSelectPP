#!/usr/bin/env python3
"""Lightweight smoke checks for the clean AdaSelect++ spine."""
from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from adaselect_pp.common import split_template_sql, sql_only, canonical_workload_line
from adaselect_pp.candidate_gen_v2.sql_evidence import StaticSQLExtractor
from adasel.ada_select import AdaSelect


def test_sql_line_helpers() -> None:
    tid, sql = split_template_sql("select * from t\t7", "q0")
    assert tid == "7" and sql.startswith("select")
    tid, sql = split_template_sql("7\tselect * from t", "q0")
    assert tid == "7" and sql.startswith("select")
    assert sql_only("7\tselect * from t") == "select * from t"
    assert canonical_workload_line("select * from t\t7", "x") == "7\tselect * from t"


def test_static_extractor_minimal() -> None:
    class DummyConn:
        def get_tables(self):
            return ["t"]
        def get_columns(self, tbl: str):
            return ["a", "b", "c"]
    ex = StaticSQLExtractor(DummyConn())
    ev = ex.extract_line("7\tselect a from t where b = 1 and c > 2", 0)
    assert ev.template_id == "7"
    assert "t" in ev.tables
    assert "b" in ev.filter_eq.get("t", [])
    assert "c" in ev.filter_rng.get("t", [])


def test_adaptive_lambda_smoke() -> None:
    tuner = AdaSelect.__new__(AdaSelect)
    tuner.alpha_init = 0.65
    tuner.lambda_policy = "adaptive"
    tuner.fixed_lambda = 0.65
    tuner.beta_error = 0.20
    tuner.lambda_min = 0.20
    tuner.lambda_max = 0.95
    tuner.ts_low = 0.50
    tuner.ts_high = 2.00
    tuner.ts_gate_regress = 0.05
    tuner.ts_mad_floor_rel = 1e-6
    tuner.ts_sign_decay = 0.90
    tuner.rsfe_decay = 0.90
    tuner.idx_alphas = {}
    tuner.idx_alphas_shadow = {}
    tuner.idx_error_smooth = {}
    tuner.idx_abs_error_smooth = {}
    tuner.idx_seen_cnt = {}
    tuner.idx_last_err_sign = {}
    tuner.idx_sign_smooth = {}
    tuner._m_stats = {}

    idx = ("t", ("b",))
    lam0, shadow0, pol0 = tuner._choose_lambda(idx, 0.0, 10.0, obs_src="OK", hit_cnt=1, ok_cnt=1)
    assert pol0 == "adaptive"
    assert 0.20 <= lam0 <= 0.95
    # A second same-direction observation should update RSFE/MAD and produce a lambda state.
    lam1, shadow1, _ = tuner._choose_lambda(idx, 1.0, 10.0, obs_src="OK", hit_cnt=1, ok_cnt=1)
    assert 0.20 <= lam1 <= 0.95
    assert idx in tuner.idx_error_smooth and idx in tuner.idx_abs_error_smooth
    # NO_HIT should be gated, not treated as a zero-benefit observation.
    before_rsfe = tuner.idx_error_smooth[idx]
    lam2, _, _ = tuner._choose_lambda(idx, 1.0, 0.0, obs_src="NO_HIT", hit_cnt=0, ok_cnt=0)
    assert 0.20 <= lam2 <= 0.95
    assert tuner.idx_error_smooth[idx] == before_rsfe


def main() -> None:
    test_sql_line_helpers()
    test_static_extractor_minimal()
    test_adaptive_lambda_smoke()
    print("Smoke check OK.")


if __name__ == "__main__":
    main()
