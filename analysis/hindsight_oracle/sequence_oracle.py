#!/usr/bin/env python3
"""Restricted hindsight sequence oracle (observed-config first pass).

This is a diagnostic oracle, not an online algorithm. It uses observed per-round
CSV rows from multiple variants and finds the best sequence among configurations
that were actually observed for each round. Costs are taken from observed rows.

Usage:
  python analysis/hindsight_oracle/sequence_oracle.py \
    --runs-root runs_hybrid_g0_attribution \
    --case job_random \
    --out oracle_job_random

Outputs:
  oracle_path.csv
  online_vs_oracle.csv
  oracle_summary.csv
"""
from __future__ import annotations

import argparse
import ast
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional

IndexConf = str

@dataclass
class Row:
    variant: str
    case: str
    round_id: int
    old: str
    new: str
    exec_ms: float
    rec_ms: float
    trans_ms: float
    total_ms: float


def _as_float(x, default=0.0):
    try:
        if x is None or x == "": return default
        return float(x)
    except Exception:
        return default


def _read_csv(path: Path, variant: str, case: str) -> List[Row]:
    out: List[Row] = []
    with path.open(newline='', encoding='utf-8') as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            rr = str(r.get('round',''))
            if not rr or rr.upper() == 'SUMMARY':
                continue
            try:
                rid = int(rr)
            except Exception:
                continue
            out.append(Row(
                variant=variant,
                case=case,
                round_id=rid,
                old=str(r.get('old','')),
                new=str(r.get('new','')),
                exec_ms=_as_float(r.get('exec')),
                rec_ms=_as_float(r.get('rec')),
                trans_ms=_as_float(r.get('trans')),
                total_ms=_as_float(r.get('total')),
            ))
    return out


def load_rows(runs_root: Path, case_filter: str) -> List[Row]:
    rows: List[Row] = []
    for csv_path in runs_root.rglob('*.csv'):
        if csv_path.name.endswith('.trace.csv'):
            continue
        if 'summary' in csv_path.name.lower():
            continue
        parts = csv_path.parts
        # expected .../<variant>/<case>/log/file.csv
        variant = ''
        case = ''
        try:
            idx = parts.index(runs_root.name)
            variant = parts[idx+1]
            case = parts[idx+2]
        except Exception:
            variant = csv_path.parent.parent.name
            case = csv_path.parent.name
        if case_filter and case_filter != case:
            continue
        rows.extend(_read_csv(csv_path, variant, case))
    return rows


def conf_size(conf_repr: str) -> int:
    try:
        x = ast.literal_eval(conf_repr)
        return len(x) if isinstance(x, list) else 0
    except Exception:
        return 0


def dp_oracle(rows: List[Row], objective: str = 'quality'):
    by_round: Dict[int, List[Row]] = {}
    for r in rows:
        by_round.setdefault(r.round_id, []).append(r)
    rounds = sorted(by_round)
    if not rounds:
        return []

    # node key = configuration repr; keep lowest execution cost row for same config at same round
    per_round: Dict[int, Dict[IndexConf, Row]] = {}
    for t in rounds:
        d: Dict[IndexConf, Row] = {}
        for r in by_round[t]:
            cur = d.get(r.new)
            cost = r.exec_ms if objective == 'exec' else (r.exec_ms + r.trans_ms)
            if cur is None:
                d[r.new] = r
            else:
                cur_cost = cur.exec_ms if objective == 'exec' else (cur.exec_ms + cur.trans_ms)
                if cost < cur_cost:
                    d[r.new] = r
        per_round[t] = d

    dp: Dict[IndexConf, float] = {}
    parent: Dict[Tuple[int, IndexConf], Optional[IndexConf]] = {}
    chosen_row: Dict[Tuple[int, IndexConf], Row] = {}

    for ti, t in enumerate(rounds):
        ndp: Dict[IndexConf, float] = {}
        for conf, r in per_round[t].items():
            stage_cost = r.exec_ms if objective == 'exec' else (r.exec_ms + r.trans_ms)
            if ti == 0:
                ndp[conf] = stage_cost
                parent[(t, conf)] = None
                chosen_row[(t, conf)] = r
            else:
                best_val = math.inf
                best_prev = None
                for prev_conf, prev_val in dp.items():
                    # First-pass observed oracle: transition is included in observed row cost.
                    # If a stronger model is needed, replace stage_cost with exec + Trans(prev, conf).
                    val = prev_val + stage_cost
                    if val < best_val:
                        best_val = val
                        best_prev = prev_conf
                ndp[conf] = best_val
                parent[(t, conf)] = best_prev
                chosen_row[(t, conf)] = r
        dp = ndp

    last_round = rounds[-1]
    best_conf = min(dp, key=lambda c: dp[c])
    path = []
    conf = best_conf
    for t in reversed(rounds):
        r = chosen_row[(t, conf)]
        path.append((t, conf, r, dp.get(conf, float('nan')) if t == last_round else float('nan')))
        conf = parent.get((t, conf))
        if conf is None and t != rounds[0]:
            break
    path.reverse()
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs-root', required=True)
    ap.add_argument('--case', required=True, help='case directory name, e.g. job_random')
    ap.add_argument('--out', required=True)
    ap.add_argument('--objective', default='quality', choices=['quality','exec'])
    args = ap.parse_args()
    runs_root = Path(args.runs_root)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(runs_root, args.case)
    if not rows:
        raise SystemExit(f'no rows found for case={args.case} under {runs_root}')
    path = dp_oracle(rows, objective=args.objective)

    with (out_dir/'oracle_path.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['round','oracle_conf','source_variant','exec','trans','rec','total','conf_size'])
        for t, conf, r, _ in path:
            w.writerow([t, conf, r.variant, r.exec_ms, r.trans_ms, r.rec_ms, r.total_ms, conf_size(conf)])

    # Compare every online variant to oracle round-by-round using observed rows.
    oracle_by_round = {t: (conf, r) for t, conf, r, _ in path}
    with (out_dir/'online_vs_oracle.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['variant','round','online_conf','oracle_conf','same_conf','online_exec','oracle_exec','online_trans','oracle_trans','exec_gap','trans_gap','total_gap'])
        for r in sorted(rows, key=lambda x:(x.variant,x.round_id)):
            if r.round_id not in oracle_by_round:
                continue
            oc, orow = oracle_by_round[r.round_id]
            w.writerow([
                r.variant, r.round_id, r.new, oc, int(r.new == oc),
                r.exec_ms, orow.exec_ms, r.trans_ms, orow.trans_ms,
                r.exec_ms-orow.exec_ms, r.trans_ms-orow.trans_ms, r.total_ms-orow.total_ms
            ])

    with (out_dir/'oracle_summary.csv').open('w', newline='', encoding='utf-8') as f:
        w=csv.writer(f)
        w.writerow(['case','objective','rounds','oracle_exec_sum','oracle_trans_sum','oracle_rec_sum','oracle_total_sum'])
        orows=[r for _,_,r,_ in path]
        w.writerow([args.case,args.objective,len(orows),sum(r.exec_ms for r in orows),sum(r.trans_ms for r in orows),sum(r.rec_ms for r in orows),sum(r.total_ms for r in orows)])

if __name__ == '__main__':
    main()
