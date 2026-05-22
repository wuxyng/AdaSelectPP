# -*- coding: utf-8 -*-
"""litesel/main.py

Phase 0.2 driver:
1) Execute each round's workload on the physical DB (db2) to obtain exec cost/time.
2) Invoke a tuner optionally (invoke_round list).
3) Apply index diffs to the physical DB (db2).
4) Record a unified per-round CSV with MetricsRecorder.

This file is intentionally kept symmetric with adasel/main.py so that
`scripts/run_baselines_snapshot.sh` can run both families with the same
post-processing.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import psycopg2.errors  # type: ignore
except Exception:  # pragma: no cover
    psycopg2 = None  # type: ignore

# Ensure project root is on path so sibling packages can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from util.metrics_recorder import MetricsRecorder
from util.trace_recorder import TraceRecorder

from database.database_connector import DatabaseConnector
from database.cost_evaluation import CostEvaluation

from litesel.mc.lite_select_mc_topk import LiteSelectMC as LiteSelectMC_topk
#from litesel.mc.lite_select_mc_twoCELF import LiteSelectMC as LiteSelectMC_twoCELF
#from litesel.lite_select_a import LiteSelectA
#from litesel.lite_select_a_2 import LiteSelectA2
#from litesel.lite_select_a_3 import LiteSelectA3
#from litesel.lite_select_a_4 import LiteSelectA4


logger = logging.getLogger(__name__)

TIMEOUT_PENALTY_MS = 30_000.0

IndexKey = Tuple[str, Tuple[str, ...]]


ALGORITHMS = {
    'LiteSelectMC_topk': LiteSelectMC_topk,
    #'LiteSelectMC_twoCELF': LiteSelectMC_twoCELF,
    #'LiteSelectA': LiteSelectA,
    #'LiteSelectA2': LiteSelectA2,
    #'LiteSelectA3': LiteSelectA3,
    #'LiteSelectA4': LiteSelectA4,
}


def _canon(idx: Tuple[Any, ...]) -> IndexKey:
    """Flattened (tbl, c1, c2, ...) -> canonical (tbl, (c1, c2, ...))."""
    if len(idx) == 2 and isinstance(idx[1], tuple):
        return idx  # already canonical
    tbl = str(idx[0])
    cols = tuple(str(c) for c in idx[1:])
    return (tbl, cols)


def _apply_create(db: DatabaseConnector, key: IndexKey) -> None:
    tbl, cols = key
    if not cols:
        return
    db.create_index(tbl, cols)


def _apply_drop(db: DatabaseConnector, key: IndexKey) -> None:
    tbl, cols = key
    if not cols:
        return
    db.drop_index(tbl, cols)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser('Online Index Tuning (LiteSelect family)')
    p.add_argument('algorithm', choices=sorted(ALGORITHMS.keys()))
    p.add_argument('benchmark')
    p.add_argument('workload_type', choices=['shifting', 'noisy', 'random', 'test'])
    p.add_argument('round_size', type=int)
    p.add_argument('invoke_round', help='Python list, e.g., "[0,1,2]" or "list(range(0,300))"')
    p.add_argument('eval_method', choices=['optimizer', 'tcnn'])

    # optional evaluator args
    p.add_argument('--cuda', action='store_true')
    p.add_argument('--episodes', type=int, default=0, help='tcnn net id (required when eval_method=tcnn)')

    # unified knobs (patched by scripts/run_baselines_snapshot.sh)
    p.add_argument('--alpha', type=float, default=None)
    p.add_argument('--beta', type=float, default=None)
    # Both --optimizer_ratio (legacy scripts) and --opratio (new scripts) map here.
    p.add_argument('--optimizer_ratio', '--optimizer-ratio', '--opratio', dest='optimizer_ratio', type=float, default=None)
    p.add_argument('--timeout', type=int, default=None, help='timeout threshold in ms')
    p.add_argument('--min_width', type=int, default=None)
    p.add_argument('--max_width', type=int, default=None)
    # Phase 0.5 option: WDCG pruning/order (plan-first)
    p.add_argument('--wdcg', action='store_true', help='Enable WDCG-based candidate pruning/order (plan-first).')
    p.add_argument('--wdcg_topk', type=int, default=None, help='Top-K candidates kept after WDCG pruning (default: 1000).')
    p.add_argument('--wdcg_family_cap', type=int, default=None, help='Per-(table, first-col) cap after WDCG ranking (default: 2).')
    p.add_argument('--wdcg_min_table_ratio', type=float, default=None, help='Small-table prune ratio relative to max table per query (default: 0.05).')
    p.add_argument('--wdcg_no_small_table_prune', action='store_true', help='Disable small-table pruning in WDCG ranking.')
    p.add_argument('--transition_mode', type=str, default=None)

    # Phase 0.3/0.4 trace
    p.add_argument(
        '--trace',
        action='store_true',
        help='Write trace CSV (per-round, per-index). Default interest set: Old ∪ New ∪ Evaluated.',
    )

    # logging/stability
    p.add_argument('--osc_window', type=int, default=20)
    return p.parse_args()


def load_workloads(path: str, round_size: int) -> List[Tuple[List[str], List[int]]]:
    """Load {benchmark}_{workload_type}.txt.

    Format is `SQL\tTEMPLATE_ID` (template id is only used for logging).
    """
    workloads: List[Tuple[List[str], List[int]]] = []
    with open(path, encoding='utf-8') as f:
        lines = [ln for ln in f.readlines() if ln.strip()]
    for i in range(0, len(lines), round_size):
        batch = lines[i:i + round_size]
        q, tid = zip(*(l.rstrip('\n').split('\t') for l in batch))
        templates = [int(x) for x in tid]
        workloads.append((list(q), templates))
    return workloads


def _stats_delta(curr: Dict[str, Any], prev: Dict[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    """Compute per-round delta from cumulative stats dict."""
    out: Dict[str, Any] = {}
    for k in keys:
        cv = curr.get(k, 0)
        pv = prev.get(k, 0)
        # ints/floats both supported
        try:
            out[k] = cv - pv
        except Exception:
            out[k] = cv
        prev[k] = cv
    return out


def _override_algo_knobs(algo: Any, args: argparse.Namespace) -> None:
    """Best-effort override (CLI wins over JSON)."""
    if args.alpha is not None and hasattr(algo, 'alpha'):
        setattr(algo, 'alpha', float(args.alpha))
    if args.beta is not None and hasattr(algo, 'beta'):
        setattr(algo, 'beta', float(args.beta))
    if args.optimizer_ratio is not None:
        if hasattr(algo, 'optimizer_ratio'):
            setattr(algo, 'optimizer_ratio', float(args.optimizer_ratio))
        elif hasattr(algo, 'ratio'):
            setattr(algo, 'ratio', float(args.optimizer_ratio))
    if args.timeout is not None and hasattr(algo, 'timeout'):
        setattr(algo, 'timeout', int(args.timeout))
    if args.min_width is not None and hasattr(algo, 'min_width'):
        setattr(algo, 'min_width', int(args.min_width))
    if args.max_width is not None and hasattr(algo, 'max_width'):
        setattr(algo, 'max_width', int(args.max_width))
    if args.transition_mode is not None and hasattr(algo, 'transition_mode'):
        setattr(algo, 'transition_mode', str(args.transition_mode))
    # Phase 0.5: WDCG knobs (only applied if algorithm exposes these attributes)
    if getattr(args, 'wdcg', False) and hasattr(algo, 'wdcg_enabled'):
        setattr(algo, 'wdcg_enabled', True)
    if getattr(args, 'wdcg_topk', None) is not None and hasattr(algo, 'wdcg_topk'):
        setattr(algo, 'wdcg_topk', int(args.wdcg_topk))
    if getattr(args, 'wdcg_family_cap', None) is not None and hasattr(algo, 'wdcg_family_cap'):
        setattr(algo, 'wdcg_family_cap', int(args.wdcg_family_cap))
    if getattr(args, 'wdcg_min_table_ratio', None) is not None and hasattr(algo, 'wdcg_min_table_ratio'):
        setattr(algo, 'wdcg_min_table_ratio', float(args.wdcg_min_table_ratio))
    if getattr(args, 'wdcg_no_small_table_prune', False) and hasattr(algo, 'wdcg_small_table_prune'):
        setattr(algo, 'wdcg_small_table_prune', False)


def main() -> None:
    args = parse_args()
    invoke_round = ast.literal_eval(args.invoke_round)

    # ---- config & log paths (script patches JSON in-place) ----
    cfg_path = Path('litesel') / 'config' / f'{args.algorithm.lower()}.json'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        cfg_path.write_text('{}', encoding='utf-8')
    try:
        cfg = json.loads(cfg_path.read_text(encoding='utf-8') or '{}')
    except Exception:
        cfg = {}

    # Apply CLI overrides to an in-memory cfg dict (do NOT mutate the JSON file).
    cfg_obj = dict(cfg) if isinstance(cfg, dict) else {}
    if args.alpha is not None:
        cfg_obj['alpha'] = float(args.alpha)
    if args.beta is not None:
        cfg_obj['beta'] = float(args.beta)
    if args.optimizer_ratio is not None:
        cfg_obj['optimizer_ratio'] = float(args.optimizer_ratio)
        cfg_obj['ratio'] = float(args.optimizer_ratio)
    if args.timeout is not None:
        cfg_obj['timeout'] = int(args.timeout)
    if args.min_width is not None:
        cfg_obj['min_width'] = int(args.min_width)
    if args.max_width is not None:
        cfg_obj['max_width'] = int(args.max_width)
    if args.transition_mode is not None:
        cfg_obj['transition_mode'] = str(args.transition_mode)

    # Phase 0.5: WDCG options (only used by algorithms that implement it)
    if getattr(args, 'wdcg', False):
        cfg_obj['wdcg'] = True
        if args.wdcg_topk is not None:
            cfg_obj['wdcg_topk'] = int(args.wdcg_topk)
        if args.wdcg_family_cap is not None:
            cfg_obj['wdcg_family_cap'] = int(args.wdcg_family_cap)
        if args.wdcg_min_table_ratio is not None:
            cfg_obj['wdcg_min_table_ratio'] = float(args.wdcg_min_table_ratio)
        if getattr(args, 'wdcg_no_small_table_prune', False):
            cfg_obj['wdcg_small_table_prune'] = False

    alpha_name = args.alpha if args.alpha is not None else cfg_obj.get('alpha', '')
    beta_name = args.beta if args.beta is not None else cfg_obj.get('beta', '')
    opr_name = args.optimizer_ratio if args.optimizer_ratio is not None else cfg_obj.get('optimizer_ratio', cfg_obj.get('ratio', ''))
    tmode_name = args.transition_mode if args.transition_mode is not None else cfg_obj.get('transition_mode', '')

    log_dir = Path('log')
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / (
        f"{args.algorithm}_{args.benchmark}_{args.workload_type}"
        f"_a{alpha_name}_b{beta_name}_op{opr_name}_mode{tmode_name}.log"
    )
    csv_path = log_path.with_suffix('.csv')
    trace_enabled = bool(args.trace or cfg_obj.get('trace', False))
    trace_path = log_path.with_suffix('.trace.csv')

    # ---- logging ----
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    fh = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    logger.info('Starting: algo=%s bench=%s type=%s round_size=%d eval=%s',
                args.algorithm, args.benchmark, args.workload_type, args.round_size, args.eval_method)
    logger.info('Config: %s', str(cfg_path))

    # ---- DB connectors & evaluator ----
    db1 = DatabaseConnector(args.benchmark, virtual=True)
    db2 = DatabaseConnector(args.benchmark, run_num=1, virtual=False)
    cost_eval = (
        CostEvaluation(db1, args.benchmark)
        if args.eval_method == 'optimizer'
        else CostEvaluation(db1, args.benchmark, args.cuda, f'tcnn/net/{args.benchmark}_{args.episodes}.pkl')
    )

    # ---- algorithm ----
    AlgoCls = ALGORITHMS[args.algorithm]
    # Prefer cfg dict injection so init-time derived state sees overridden knobs.
    try:
        algo = AlgoCls(args.benchmark, cost_eval, db1, db2, cfg_source=cfg_obj)
    except TypeError:
        try:
            algo = AlgoCls(args.benchmark, cost_eval, db1, db2, cfg_path=str(cfg_path))
        except TypeError:
            algo = AlgoCls(args.benchmark, cost_eval, db1, db2)
    _override_algo_knobs(algo, args)

    wl_path = f'database/workload/{args.benchmark}_{args.workload_type}.txt'
    workloads = load_workloads(wl_path, args.round_size)

    # A-metrics: record per-round deltas from cumulative counters
    stat_keys = [
        'what_if_calls', 'reconf_add', 'reconf_drop', 'trans_create', 'trans_drop',
        'candidate_count', 'evaluated_count',
    ]
    prev_stats: Dict[str, Any] = {k: 0 for k in stat_keys}

    tot_exec = tot_rec = tot_trans = tot_total = 0.0
    exit_code = 0
    message = ''

    trace_ctx = TraceRecorder(str(trace_path), flush_each_row=True) if trace_enabled else nullcontext()

    with MetricsRecorder(str(csv_path), osc_window=args.osc_window, flush_each_row=True) as recorder, trace_ctx as tracer:
        for rnd, (queries, templates) in enumerate(workloads):
            logger.info('----- Round %d ----- templates=%s', rnd, templates)

            # 1) execute workload under *current* physical config (db2)
            runtimes: List[float] = []
            round_timeout = 0
            for q in queries:
                try:
                    rt = db2.get_query_runtime(q)
                    runtimes.append(float(rt))
                except psycopg2.errors.QueryCanceled:
                    runtimes.append(TIMEOUT_PENALTY_MS)
                    round_timeout = 1
                except psycopg2.errors.InFailedSqlTransaction:
                    try:
                        db2.rollback()
                    except Exception:
                        pass
                    runtimes.append(TIMEOUT_PENALTY_MS)
                    round_timeout = 1
                except Exception as e:
                    logger.warning('Runtime error, using penalty: %s', e)
                    try:
                        db2.rollback()
                    except Exception:
                        pass
                    runtimes.append(TIMEOUT_PENALTY_MS)
                    round_timeout = 1
            exec_ms = float(sum(runtimes))

            # 2) reset virtual indexes and snapshot current physical config
            try:
                db1.drop_all_indexes()
            except Exception:
                pass
            old_flat = set(db2.get_indexes())
            old_conf: Set[IndexKey] = {_canon(i) for i in old_flat}
            saved_old = set(old_conf)

            # 3) invoke tuner
            t_rec0 = time.perf_counter()
            if rnd in invoke_round:
                try:
                    new_conf = algo.run(queries, old_conf, [int(x) for x in runtimes])
                except Exception as e:
                    logger.exception('Algo crashed at round %d: %s', rnd, e)
                    new_conf = set(old_conf)
                    exit_code = 2
                    message = f'round={rnd} algo_error={e}'
            else:
                new_conf = set(old_conf)
            rec_ms = (time.perf_counter() - t_rec0) * 1000.0

            # 4) apply diff to physical db2
            t_tr0 = time.perf_counter()
            try:
                drop_set = saved_old - set(new_conf)
                add_set = set(new_conf) - saved_old
                for k in sorted(drop_set):
                    _apply_drop(db2, k)
                for k in sorted(add_set):
                    _apply_create(db2, k)
            except Exception as e:
                logger.exception('DDL apply failed at round %d: %s', rnd, e)
                try:
                    db2.rollback()
                except Exception:
                    pass
                exit_code = 3
                message = f'round={rnd} ddl_error={e}'
            trans_ms = (time.perf_counter() - t_tr0) * 1000.0
            total_ms = exec_ms + rec_ms + trans_ms

            switched = 1 if saved_old != set(new_conf) else 0

            # 5) per-round A-metrics
            curr = getattr(algo, '_m_stats', {}) or {}
            delta = _stats_delta(curr, prev_stats, stat_keys)

            # 6) write row
            recorder.record_round(
                round_id=rnd,
                exec_ms=exec_ms,
                rec_ms=rec_ms,
                trans_ms=trans_ms,
                total_ms=total_ms,
                old_conf=saved_old,
                new_conf=set(new_conf),
                switched=switched,
                beta=getattr(algo, 'beta', None),
                alpha_ema=getattr(algo, 'alpha', None),
                what_if_calls=_safe_int(delta.get('what_if_calls')),
                candidate_count=_safe_int(delta.get('candidate_count')),
                evaluated_count=_safe_int(delta.get('evaluated_count')),
                reconf_add=_safe_int(delta.get('reconf_add')),
                reconf_drop=_safe_int(delta.get('reconf_drop')),
                trans_create=_safe_float(delta.get('trans_create')),
                trans_drop=_safe_float(delta.get('trans_drop')),
                avg_sigma=getattr(algo, 'avg_sigma', None),
                sigma_epi=getattr(algo, 'sigma_epi', None),
                sigma_drift=getattr(algo, 'sigma_drift', None),
                freeze_flag=getattr(algo, 'freeze_flag', None),
                drift_flag=getattr(algo, 'drift_flag', None),
                regime=getattr(algo, 'regime', None),
                timeout=round_timeout,
                exit_code=exit_code,
                message=message,
            )

            # Phase 0.3/0.4 trace: per-round per-index rows.
            if tracer is not None:
                evaluated_set = set()
                if rnd in invoke_round:
                    try:
                        evaluated_set = set(getattr(algo, '_last_evaluated_set', set()))
                    except Exception:
                        evaluated_set = set()
                tracer.record_round(
                    round_id=rnd,
                    old_conf=saved_old,
                    new_conf=set(new_conf),
                    evaluated_set=evaluated_set,
                    tuner=algo if (rnd in invoke_round) else None,
                    workload_templates=templates,
                    invoke_flag=int(rnd in invoke_round),
                    timeout=round_timeout,
                )

            tot_exec += exec_ms
            tot_rec += rec_ms
            tot_trans += trans_ms
            tot_total += total_ms

        # summary
        recorder.write_summary(
            exec_sum=tot_exec,
            rec_sum=tot_rec,
            trans_sum=tot_trans,
            total_sum=tot_total,
            what_if_total=_safe_int(prev_stats.get('what_if_calls')),
            switched_total=None,
            exit_code=exit_code,
            message=message,
        )

    try:
        db1.close()
    except Exception:
        pass
    try:
        db2.close()
    except Exception:
        pass
    logger.info('Done. csv=%s', str(csv_path))


if __name__ == '__main__':
    main()
