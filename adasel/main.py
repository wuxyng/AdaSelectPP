# -*- coding: utf-8 -*-
"""adasel/main.py

Phase 0.2 driver:
1) Execute each round's workload on the physical DB (db2) to obtain exec cost/time.
2) Invoke AdaSelect optionally (invoke_round list).
3) Apply index diffs to the physical DB (db2).
4) Record a unified per-round CSV with MetricsRecorder.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from time import perf_counter
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database.cost_evaluation import CostEvaluation
from database.database_connector import DatabaseConnector
from util.metrics_recorder import MetricsRecorder
from util.trace_recorder import TraceRecorder
from util.logging_utils import setup_logging
from adasel.ada_select import AdaSelect
from adaselect_pp.common import canonical_workload_line, sql_only


IndexKey = Tuple[str, Tuple[str, ...]]
TIMEOUT_PENALTY_MS = 30_000.0


def _canon_from_flat(idx_flat: Tuple[str, ...]) -> IndexKey:
    tbl = idx_flat[0]
    cols = tuple(idx_flat[1:])
    return tbl, cols


def _parse_rounds(spec: str, round_size: int) -> Set[int]:
    """Parse invoke_round spec.

    Supported forms:
      - "all"
      - "none"
      - "range:2" (every 2 rounds)
      - "list:0,3,5"
    """
    spec = (spec or "all").strip().lower()
    if spec == "all":
        return set(range(round_size))
    if spec == "none":
        return set()
    if spec.startswith("range:"):
        try:
            k = int(spec.split(":", 1)[1])
            k = max(1, k)
            return set(range(0, round_size, k))
        except Exception:
            return set(range(round_size))
    if spec.startswith("list:"):
        try:
            xs = spec.split(":", 1)[1]
            return {int(x) for x in xs.split(",") if x.strip() != ""}
        except Exception:
            return set()
    # fallback
    return set(range(round_size))


def load_workloads(bench: str, wtype: str, round_size: int) -> List[List[str]]:
    """Load workloads from database/workload/{bench}_{wtype}.txt.

    Workload files may store either <SQL>\t<template_id> or
    <template_id>\t<SQL>.  The internal representation is canonicalized to
    <template_id>\t<SQL>.  Database execution paths strip the template id by
    using adaselect_pp.common.sql_only().
    """
    path = Path("database") / "workload" / f"{bench}_{wtype}.txt"
    if not path.exists():
        raise FileNotFoundError(f"workload file not found: {path}")

    workload_all: List[List[str]] = []
    cur: List[str] = []
    line_no = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip("\n")
            if not line:
                continue
            cur.append(canonical_workload_line(line, fallback_id=str(line_no)))
            line_no += 1
            if len(cur) >= round_size:
                workload_all.append(cur)
                cur = []
    if cur:
        workload_all.append(cur)
    return workload_all

def _delta_stats(curr: Dict[str, float], prev: Dict[str, float], keys: Sequence[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k in keys:
        try:
            out[k] = float(curr.get(k, 0.0)) - float(prev.get(k, 0.0))
        except Exception:
            out[k] = 0.0
    return out


def _update_prev(prev: Dict[str, float], curr: Dict[str, float], keys: Sequence[str]) -> None:
    for k in keys:
        try:
            prev[k] = float(curr.get(k, 0.0))
        except Exception:
            prev[k] = prev.get(k, 0.0)


def _ensure_json(path: Path, default_obj: Optional[dict] = None) -> None:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                _ = json.load(f)
            return
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(default_obj or {}, f, indent=2)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("algorithm")
    p.add_argument("benchmark")
    p.add_argument("workload_type")
    p.add_argument("round_size", type=int)
    p.add_argument("invoke_round")
    p.add_argument("eval_method", choices=["optimizer", "tcnn"], default="optimizer")

    # execution / evaluation
    p.add_argument("--cuda", action="store_true")
    p.add_argument("--episodes", type=int, default=0)

    # common knobs (run_baselines_snapshot.sh patches these into config)
    p.add_argument("--alpha", type=float, default=None)
    p.add_argument("--beta", type=float, default=None)
    # Both --optimizer_ratio (legacy scripts) and --opratio (new scripts) map here.
    p.add_argument("--optimizer_ratio", "--optimizer-ratio", "--opratio", dest="optimizer_ratio", type=float, default=None)
    p.add_argument("--timeout", type=int, default=None)  # ms
    p.add_argument("--min_width", type=int, default=None)
    p.add_argument("--max_width", type=int, default=None)
    p.add_argument("--rsfe_decay", type=float, default=None)

    # lambda policy (AdaSelect: adaptive vs fixed)
    p.add_argument("--lambda_policy", type=str, choices=["adaptive", "fixed"], default=None)
    p.add_argument("--fixed_lambda", type=float, default=None, help=argparse.SUPPRESS)

    # WDCG enable (Phase 0.5 funnel)
    p.add_argument("--wdcg_enabled", "--wdcg-enabled", dest="wdcg_enabled", type=int, choices=[0,1], default=None)

    # recorder
    p.add_argument("--osc_window", type=int, default=20)
    p.add_argument(
        "--trace",
        action="store_true",
        help="Write trace CSV (per-round, per-index). Default interest set: Old ∪ New ∪ Evaluated.",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging (more verbose, with file/line context).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    # logging is configured after loading cfg + CLI overrides (so log stem matches run).
    logger = logging.getLogger(__name__)


    # config path (must match run_baselines_snapshot.sh)
    cfg_path = Path('adasel') / 'config' / f'{args.algorithm.lower()}.json'
    _ensure_json(
        cfg_path,
        default_obj={
            "max_num": 10,
            "alpha": 0.65,
            "beta": 1.1,
            "optimizer_ratio": 0.5,
            "ratio": 0.5,
            "timeout": 30000,
            "min_width": 1,
            "max_width": 2,
            "rsfe_decay": 0.9,
        },
    )

    # Load cfg once and apply CLI overrides to cfg dict (without mutating the JSON file).
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg_obj = json.load(f) or {}
    except Exception:
        cfg_obj = {}
    if args.alpha is not None:
        cfg_obj["alpha"] = float(args.alpha)
    if args.beta is not None:
        cfg_obj["beta"] = float(args.beta)
    if args.optimizer_ratio is not None:
        # AdaSelect historically used "ratio"; keep both keys.
        cfg_obj["optimizer_ratio"] = float(args.optimizer_ratio)
        cfg_obj["ratio"] = float(args.optimizer_ratio)
    if args.timeout is not None:
        cfg_obj["timeout"] = int(args.timeout)
    if args.min_width is not None:
        cfg_obj["min_width"] = int(args.min_width)
    if args.max_width is not None:
        if int(args.max_width) > 2:
            raise ValueError("Phase 0.5 AdaSelect-PG supports max_width <= 2 only")
        cfg_obj["max_width"] = int(args.max_width)
    if args.rsfe_decay is not None:
        cfg_obj["rsfe_decay"] = float(args.rsfe_decay)

    # Phase 0.5 knobs (lambda_policy / fixed_lambda / WDCG toggle)
    if args.lambda_policy is not None:
        cfg_obj["lambda_policy"] = str(args.lambda_policy).lower()
    # Compatibility: --fixed_lambda is treated as an alias of --alpha (deprecated/hidden).
    fixed_lambda_mismatch = False
    if args.fixed_lambda is not None:
        if args.alpha is None:
            cfg_obj["alpha"] = float(args.fixed_lambda)
        else:
            try:
                fixed_lambda_mismatch = abs(float(args.fixed_lambda) - float(cfg_obj.get("alpha", args.alpha))) > 1e-12
            except Exception:
                fixed_lambda_mismatch = True
    if args.wdcg_enabled is not None:
        cfg_obj["wdcg_enabled"] = bool(int(args.wdcg_enabled))
    if not bool(cfg_obj.get("wdcg_enabled", True)):
        raise ValueError("wdcg_enabled=false is not supported by the active Phase 0.5 generator path")


    # ---- logging (stdout + file; log/csv share the same stem) ----
    alpha_name = args.alpha if args.alpha is not None else cfg_obj.get('alpha', '')
    beta_name = args.beta if args.beta is not None else cfg_obj.get('beta', '')
    opr_name = args.optimizer_ratio if args.optimizer_ratio is not None else cfg_obj.get('optimizer_ratio', cfg_obj.get('ratio', ''))
    # Include lambda/WDCG tags in filenames to prevent overwriting between sweep runs.
    lam_policy = str(cfg_obj.get('lambda_policy', 'adaptive')).lower()
    try:
        alpha_f = float(cfg_obj.get('alpha', ''))
    except Exception:
        alpha_f = None
    if lam_policy in ('fixed', 'fix', 'const', 'constant'):
        # In fixed policy, the constant lambda is the case-level alpha.
        lam_tag = f"lamfixed{alpha_f:.3f}" if alpha_f is not None else 'lamfixed'
    else:
        lam_tag = 'lamadaptive'
    wdcg_on = 1 if bool(cfg_obj.get('wdcg_enabled', False)) else 0
    wdcg_tag = f"wdcg{wdcg_on}"
    log_dir = Path('log')
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / (
        f"{args.algorithm.lower()}_{args.benchmark}_{args.workload_type}_a{alpha_name}_b{beta_name}_op{opr_name}_{lam_tag}_{wdcg_tag}.log"
    )
    csv_path = log_path.with_suffix('.csv')

    setup_logging(log_path, debug=bool(args.debug))

    logger = logging.getLogger(__name__)
    if 'fixed_lambda_mismatch' in locals() and fixed_lambda_mismatch:
        logger.warning('CLI provided both --alpha and deprecated --fixed_lambda with different values; ignoring --fixed_lambda and using alpha=%s', str(cfg_obj.get('alpha')))
    logger.info('Starting: algo=%s bench=%s type=%s round_size=%d eval=%s',
                args.algorithm, args.benchmark, args.workload_type, args.round_size, args.eval_method)
    logger.info('Config: %s', str(cfg_path))
    logger.info('Log: %s', str(log_path))
    logger.info('CSV: %s', str(csv_path))

    # instantiate DBs
    db1 = DatabaseConnector(args.benchmark, virtual=True, run_num=1)
    db2 = DatabaseConnector(args.benchmark, virtual=False, run_num=1)
    # --- Build CostEvaluation (MATCHES signature: CostEvaluation(db_con, benchmark, cuda=True, net_file=None)) ---
    m = str(args.eval_method).lower()

    # Which DB connector is used for cost evaluation?
    # - optimizer / whatif -> use virtual connector (db1)
    # - actual runtimes    -> use physical connector (db2)
    if m in ("optimizer", "whatif", "tcnn"):
        ce_db = db1
    elif m in ("actual", "actual_runtimes", "runtimes", "runtime"):
        ce_db = db2
    else:
        raise ValueError(f"Unknown eval_method={args.eval_method}. Use optimizer|whatif|actual_runtimes.")

    # Optional learned cost model file (if present in cfg_obj)
    net_file = None
    if isinstance(cfg_obj, dict):
        net_file = cfg_obj.get("net_file") or cfg_obj.get("net")

    cost_eval = CostEvaluation(
        ce_db,
        args.benchmark,
        cuda=bool(getattr(args, "cuda", True)),
        net_file=net_file,
    )
    # --- end CostEvaluation ---

    # build tuner
    # Prefer cfg dict injection so that init-time derived states see overridden knobs.
    try:
        tuner = AdaSelect(args.benchmark, cost_eval, db1, db2, cfg_source=cfg_obj)
    except TypeError:
        # Backward-compat: old signature only accepts cfg_path
        tuner = AdaSelect(args.benchmark, cost_eval, db1, db2, cfg_path=str(cfg_path))

    # CLI overrides (optional)
    if args.alpha is not None and hasattr(tuner, "alpha_init"):
        tuner.alpha_init = float(args.alpha)
    if args.beta is not None and hasattr(tuner, "beta"):
        tuner.beta = float(args.beta)
    if args.optimizer_ratio is not None:
        # AdaSelect historically uses "ratio"; keep both.
        if hasattr(tuner, "ratio"):
            tuner.ratio = float(args.optimizer_ratio)
        if hasattr(tuner, "optimizer_ratio"):
            tuner.optimizer_ratio = float(args.optimizer_ratio)
    if args.timeout is not None and hasattr(tuner, "timeout"):
        tuner.timeout = int(args.timeout)
    if args.min_width is not None and hasattr(tuner, "min_width"):
        tuner.min_width = int(args.min_width)
    if args.max_width is not None and hasattr(tuner, "max_width"):
        if int(args.max_width) > 2:
            raise ValueError("Phase 0.5 AdaSelect-PG supports max_width <= 2 only")
        tuner.max_width = int(args.max_width)
    if args.rsfe_decay is not None and hasattr(tuner, "rsfe_decay"):
        tuner.rsfe_decay = float(args.rsfe_decay)
    # output paths (csv shares the same stem as log_path)
    # NOTE: filenames include lam_tag/wdcg_tag to avoid overwriting between sweep runs.
    csv_path = log_path.with_suffix('.csv')
    trace_enabled = bool(args.trace or cfg_obj.get('trace', False))
    trace_path = log_path.with_suffix('.trace.csv')
    recorder = MetricsRecorder(str(csv_path), osc_window=args.osc_window, flush_each_row=True)
    tracer = TraceRecorder(trace_path, flush_each_row=True) if trace_enabled else None
    if tracer is not None:
        tracer.__enter__()

    # workloads and invocation schedule
    workloads = load_workloads(args.benchmark, args.workload_type, args.round_size)
    invoke_set = _parse_rounds(args.invoke_round, len(workloads))

    # cumulative→per-round delta stats
    stat_keys = [
        "what_if_calls",
        "candidate_count",
        "evaluated_count",
        "filtered_nonpositive_count",
        "reconf_add",
        "reconf_drop",
        "trans_create",
        "trans_drop",
    ]
    prev_stats: Dict[str, float] = {k: 0.0 for k in stat_keys}

    # totals for summary
    tot_exec = tot_rec = tot_trans = tot_total = 0.0
    tot_whatif = tot_cand = tot_eval = 0.0

    for rid, workload in enumerate(workloads):
        # (1) execute workload under current physical config
        runtimes: List[float] = []
        timeout_round = 0
        exec_start = perf_counter()
        for q in workload:
            try:
                rt = float(db2.get_query_runtime(sql_only(q)))  # ms
                runtimes.append(rt)
            except Exception:
                runtimes.append(TIMEOUT_PENALTY_MS)
                timeout_round = 1
        exec_ms = float(sum(runtimes))
        exec_wall_ms = (perf_counter() - exec_start) * 1000.0

        # (2) drop virtual indexes
        try:
            db1.drop_all_indexes()
        except Exception:
            pass

        # (3) snapshot old physical config
        old_flat = set(db2.get_indexes())
        old_conf: Set[IndexKey] = {_canon_from_flat(t) for t in old_flat}
        saved_old = set(old_conf)
        # (4) tune
        invoke_flag = (rid in invoke_set)
        rec_start = perf_counter()
        if invoke_flag:
            try:
                new_conf = tuner.run(workload, old_conf, [int(x) for x in runtimes])
            except Exception as e:
                logger.exception("tuner.run failed at round %s: %s", rid, e)
                new_conf = set(old_conf)
        else:
            new_conf = set(old_conf)

        # Robustness: tuner.run implementations in this repo are not consistent
        # about returning `set` vs `list`. Normalize here so diff logic is safe.
        if not isinstance(new_conf, set):
            try:
                new_conf = set(new_conf)
            except Exception:
                new_conf = set(old_conf)
        rec_ms = (perf_counter() - rec_start) * 1000.0

        # (5) apply physical diff
        trans_start = perf_counter()
        to_drop = old_conf - new_conf
        to_add = new_conf - old_conf
        for (tbl, cols) in to_drop:
            try:
                db2.drop_index(tbl, cols)
            except Exception:
                pass
        for (tbl, cols) in to_add:
            try:
                db2.create_index(tbl, cols)
            except Exception:
                pass
        trans_ms = (perf_counter() - trans_start) * 1000.0

        total_ms = exec_ms + rec_ms + trans_ms
        switched = 1 if saved_old != set(new_conf) else 0

        # (6) per-round A-metrics (delta)
        curr_stats = getattr(tuner, "_m_stats", {}) or {}
        d = _delta_stats(curr_stats, prev_stats, stat_keys)
        _update_prev(prev_stats, curr_stats, stat_keys)

        # Phase 0.5 (WDCG): extract funnel metrics (absolute, per round)
        wdcg_stats = {}
        try:
            wdcg_stats = getattr(tuner, "_last_wdcg_stats", {}) or {}
        except Exception:
            wdcg_stats = {}

        # Stability: dead-zone support-gate stats (AdaSelect)
        deadzone_stats = {}
        try:
            deadzone_stats = getattr(tuner, "_last_deadzone_stats", {}) or {}
        except Exception:
            deadzone_stats = {}

        # (7) record
        recorder.record_round(
            round_id=rid,
            exec_ms=exec_ms,
            rec_ms=rec_ms,
            trans_ms=trans_ms,
            total_ms=total_ms,
            what_if_calls=d.get("what_if_calls", 0.0),
            candidate_count=d.get("candidate_count", 0.0),
            evaluated_count=d.get("evaluated_count", 0.0),
            filtered_nonpositive_count=d.get("filtered_nonpositive_count", 0.0),
            preconf_count=len(getattr(tuner, "_last_candidate_conf", set()) or set()),
            candidate_count_raw=wdcg_stats.get("candidate_count_raw", None),
            wdcg_pruned_count=wdcg_stats.get("wdcg_pruned_count", None),
            wdcg_selected_post_compile=wdcg_stats.get("wdcg_selected_post_compile", None),
            merged_total=wdcg_stats.get("merged_total", None),
            merged_group=wdcg_stats.get("merged_group", None),
            merged_order=wdcg_stats.get("merged_order", None),
            merged_covering=wdcg_stats.get("merged_covering", None),
            compile_validation_enabled=wdcg_stats.get("compile_validation_enabled", None),
            compile_validation_passes=wdcg_stats.get("compile_validation_passes", None),
            compile_validation_trials=wdcg_stats.get("compile_validation_trials", None),
            compile_validated=wdcg_stats.get("compile_validated", None),
            compile_invalidated=wdcg_stats.get("compile_invalidated", None),
            compile_errors=wdcg_stats.get("compile_errors", None),
            compile_not_picked=wdcg_stats.get("compile_not_picked", None),
            pruned_small_tables=wdcg_stats.get("pruned_small_tables", None),
            skipped_high_dml_tables=wdcg_stats.get("skipped_high_dml_tables", None),
            dml_tables_downweighted=wdcg_stats.get("dml_tables_downweighted", None),
            dml_weight_min=wdcg_stats.get("dml_weight_min", None),
            dml_weight_max=wdcg_stats.get("dml_weight_max", None),
            coverage_ratio=wdcg_stats.get("coverage_ratio", None),
            wdcg_elapsed_ms=wdcg_stats.get("wdcg_elapsed_ms", None),
            parse_ast_ok=wdcg_stats.get("parse_ast_ok", None),
            parse_fallback_regex=wdcg_stats.get("parse_fallback_regex", None),
            gen_mode=wdcg_stats.get("gen_mode", None),
            probe_rounds=wdcg_stats.get("probe_rounds", None),
            workload_count=wdcg_stats.get("workload_count", None),
            width1_count=wdcg_stats.get("width1_count", None),
            width2_count=wdcg_stats.get("width2_count", None),
            seed_count=wdcg_stats.get("seed_count", None),
            eligible_seed_count=wdcg_stats.get("eligible_seed_count", None),
            multi_growth_count=wdcg_stats.get("multi_growth_count", None),
            rejected_growth_has_or=wdcg_stats.get("rejected_growth_has_or", None),
            rejected_growth_alias_ambiguous=wdcg_stats.get("rejected_growth_alias_ambiguous", None),
            rejected_growth_seed_not_positive=wdcg_stats.get("rejected_growth_seed_not_positive", None),
            rejected_growth_seed_unseen=wdcg_stats.get("rejected_growth_seed_unseen", None),
            rejected_growth_range_seed=wdcg_stats.get("rejected_growth_range_seed", None),
            rejected_growth_parse_fallback=wdcg_stats.get("rejected_growth_parse_fallback", None),
            family_eq1=wdcg_stats.get("family_eq1", None),
            family_join_eq1=wdcg_stats.get("family_join_eq1", None),
            family_range1=wdcg_stats.get("family_range1", None),
            family_eqeq=wdcg_stats.get("family_eqeq", None),
            family_eqrange=wdcg_stats.get("family_eqrange", None),
            family_rescue=wdcg_stats.get("family_rescue", None),
            source_ast=wdcg_stats.get("source_ast", None),
            source_strong_ast=wdcg_stats.get("source_strong_ast", None),
            source_static_fallback=wdcg_stats.get("source_static_fallback", None),
            source_vacuum_rescue=wdcg_stats.get("source_vacuum_rescue", None),
            vocab_enabled=wdcg_stats.get("vocab_enabled", None),
            vocab_tables=wdcg_stats.get("vocab_tables", None),
            vocab_columns=wdcg_stats.get("vocab_columns", None),
            wdcg_skipped_family=wdcg_stats.get("wdcg_skipped_family", None),
            wdcg_skipped_dominated=wdcg_stats.get("wdcg_skipped_dominated", None),
            coverage_boost_added=wdcg_stats.get("coverage_boost_added", None),
            wdcg_warmup_active=wdcg_stats.get("wdcg_warmup_active", None),
            structural_pair_quota=wdcg_stats.get("structural_pair_quota", None),
            structural_pair_eval_count=wdcg_stats.get("structural_pair_eval_count", None),
            structural_pair_eval_selected_keys=wdcg_stats.get("structural_pair_eval_selected_keys", None),
            structural_pair_eval_budgeted_out_count=wdcg_stats.get("structural_pair_eval_budgeted_out_count", None),
            structural_pair_eval_lane_enabled=wdcg_stats.get("structural_pair_eval_lane_enabled", None),
            aff_avg=wdcg_stats.get("aff_avg", None),
            aff_p90=wdcg_stats.get("aff_p90", None),
            aff_max=wdcg_stats.get("aff_max", None),
            predicted_what_if_calls=wdcg_stats.get("predicted_what_if_calls", None),
            aff_top=wdcg_stats.get("suspicious_aff_top", None),
            aff_suspicious_frac=(
                float(wdcg_stats.get("suspicious_aff_count", 0.0))
                / float(max(1.0, float(wdcg_stats.get("wdcg_pruned_count", 0.0) or 0.0)))
                if wdcg_stats.get("suspicious_aff_count", None) is not None
                else None
            ),
            reconf_add=d.get("reconf_add", 0.0),
            reconf_drop=d.get("reconf_drop", 0.0),
            trans_create=d.get("trans_create", 0.0),
            trans_drop=d.get("trans_drop", 0.0),
            deadzone_old_support=deadzone_stats.get("deadzone_old_support", None),
            deadzone_blocked=deadzone_stats.get("deadzone_blocked", None),
            decision_ratio=(getattr(tuner, "_last_decision_stats", {}) or {}).get("ratio", None),
            decision_old_benefit=(getattr(tuner, "_last_decision_stats", {}) or {}).get("old_benefit", None),
            decision_new_benefit=(getattr(tuner, "_last_decision_stats", {}) or {}).get("new_benefit", None),
            corr_trials=wdcg_stats.get("corr_trials", None),
            old_relevant_count=wdcg_stats.get("old_relevant_count", None),
            old_relevant_not_appearing_count=wdcg_stats.get("old_relevant_not_appearing_count", None),
            alpha_ema=getattr(tuner, "alpha_init", None),
            lambda_ctrl=getattr(tuner, "lambda_ctrl", None),
            beta=getattr(tuner, "beta", None),
            switched=switched,
            old_conf=saved_old,
            new_conf=set(new_conf),
            timeout=timeout_round,
        )

        # Phase 0.3/0.4 trace: per-round per-index rows.
        if tracer is not None:
            evaluated_set: Set[IndexKey] = set()
            if invoke_flag:
                try:
                    evaluated_set = set(getattr(tuner, '_last_evaluated_set', set()))
                except Exception:
                    evaluated_set = set()
            # TraceRecorder schema is stable and does not depend on workload templates.
            tracer.record_round(
                round_id=rid,
                algo_name=str(args.algorithm).lower(),
                old_conf=saved_old,
                new_conf=set(new_conf),
                evaluated_set=evaluated_set,
                tuner=tuner if invoke_flag else None,
            )

        tot_exec += exec_ms
        tot_rec += rec_ms
        tot_trans += trans_ms
        tot_total += total_ms
        tot_whatif += d.get("what_if_calls", 0.0)
        tot_cand += d.get("candidate_count", 0.0)
        tot_eval += d.get("evaluated_count", 0.0)

        # (optional) log wall-clock of executing queries (for debugging)
        logger.info(
            "round=%s exec_ms=%.2f exec_wall_ms=%.2f rec_ms=%.2f trans_ms=%.2f total_ms=%.2f switched=%s timeout=%s",
            rid,
            exec_ms,
            exec_wall_ms,
            rec_ms,
            trans_ms,
            total_ms,
            switched,
            timeout_round,
        )

    # summary row
    recorder.write_summary(
        exec_sum=tot_exec,
        rec_sum=tot_rec,
        trans_sum=tot_trans,
        total_sum=tot_total,
        whatif_sum=tot_whatif,
        cand_sum=tot_cand,
        eval_sum=tot_eval,
    )

    try:
        recorder.close()
    except Exception:
        pass

    if tracer is not None:
        try:
            tracer.__exit__(None, None, None)
        except Exception:
            pass

    try:
        db1.close()
        db2.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
