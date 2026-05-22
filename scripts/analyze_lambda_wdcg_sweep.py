#!/usr/bin/env python3
"""Analyze 4-combo sweep runs of AdaSelect++ (lambda_policy x wdcg_enabled).

This script expects per-run CSVs produced by adasel/main.py, with filenames like:
  log/adaselect_<bench>_<wtype>_a..._b..._op..._lam<...>_wdcg<0|1>.csv

Usage examples:
  python scripts/analyze_lambda_wdcg_sweep.py --glob "log/adaselect_tpch_shifting_*_lam*_wdcg*.csv"
  python scripts/analyze_lambda_wdcg_sweep.py --bench tpch --wtype shifting

Outputs:
  - prints a summary table
  - writes figures to analysis/ (total/exec/rec curves)
"""

from __future__ import annotations

import argparse
import glob
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import matplotlib.pyplot as plt


@dataclass
class RunId:
    bench: str
    wtype: str
    lam_policy: str
    fixed_lam: Optional[float]
    wdcg_on: int
    path: str


RE_NAME = re.compile(
    r"^(?P<algo>[^_]+)_(?P<bench>[^_]+)_(?P<wtype>[^_]+)_a(?P<a>[^_]+)_b(?P<b>[^_]+)_op(?P<op>[^_]+)_(?P<lam>lam[^_]+)_(?P<wdcg>wdcg[01])$"
)


def parse_run_id(csv_path: str) -> Optional[RunId]:
    stem = Path(csv_path).with_suffix("").name
    m = RE_NAME.match(stem)
    if not m:
        return None
    bench = m.group("bench")
    wtype = m.group("wtype")
    lam_tag = m.group("lam")
    wdcg_tag = m.group("wdcg")

    wdcg_on = 1 if wdcg_tag.endswith("1") else 0

    # lam_tag forms:
    #   lamadaptive
    #   lamfixed0.650
    #   lamfixed
    lam_policy = "adaptive"
    fixed_lam = None
    if lam_tag.startswith("lamfixed"):
        lam_policy = "fixed"
        suf = lam_tag[len("lamfixed") :]
        if suf:
            try:
                fixed_lam = float(suf)
            except Exception:
                fixed_lam = None
    elif lam_tag.startswith("lam"):
        lam_policy = lam_tag[len("lam") :]

    return RunId(bench=bench, wtype=wtype, lam_policy=lam_policy, fixed_lam=fixed_lam, wdcg_on=wdcg_on, path=csv_path)


def _safe_float_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def load_one(run: RunId) -> pd.DataFrame:
    df = pd.read_csv(run.path)

    # IMPORTANT: per_round.csv contains a trailing SUMMARY row (round=SUMMARY)
    # which stores totals. Never treat it as a normal round, otherwise averages
    # will be badly skewed ("double counting" the totals).
    if "round" in df.columns:
        round_raw = df["round"].astype(str)
        round_num = pd.to_numeric(df["round"], errors="coerce")
        drop_mask = round_raw.str.upper().eq("SUMMARY") | round_num.isna()
        if drop_mask.any():
            df = df.loc[~drop_mask].copy()
        df["round"] = round_num.loc[~drop_mask].astype(float)

    # Ensure core numeric columns (except round which is handled above)
    for c in ["exec", "rec", "trans", "total", "timeout", "what_if_calls", "candidate_count", "evaluated_count", "oscillation_rate", "stability_score"]:
        if c in df.columns:
            df[c] = _safe_float_series(df[c])
    df["bench"] = run.bench
    df["wtype"] = run.wtype
    df["lam_policy"] = run.lam_policy
    df["fixed_lam"] = run.fixed_lam if run.fixed_lam is not None else ""
    df["wdcg_on"] = run.wdcg_on
    df["run"] = os.path.basename(run.path)
    return df


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    gcols = ["bench", "wtype", "lam_policy", "fixed_lam", "wdcg_on", "run"]
    rows = []
    for keys, d in df.groupby(gcols, dropna=False):
        bench, wtype, lam_policy, fixed_lam, wdcg_on, run = keys
        total = d["total"]
        execv = d.get("exec", pd.Series([], dtype=float))
        recv = d.get("rec", pd.Series([], dtype=float))
        trans = d.get("trans", pd.Series([], dtype=float))
        timeouts = int(d.get("timeout", pd.Series([], dtype=float)).sum())

        row = {
            "bench": bench,
            "wtype": wtype,
            "lam_policy": lam_policy,
            "fixed_lam": fixed_lam,
            "wdcg_on": int(wdcg_on),
            "rounds": int(len(d)),
            "total_avg": float(total.mean()) if len(total) else 0.0,
            "total_p50": float(total.quantile(0.50)) if len(total) else 0.0,
            "total_p90": float(total.quantile(0.90)) if len(total) else 0.0,
            "exec_avg": float(execv.mean()) if len(execv) else 0.0,
            "rec_avg": float(recv.mean()) if len(recv) else 0.0,
            "trans_avg": float(trans.mean()) if len(trans) else 0.0,
            "timeouts": timeouts,
            "what_if_sum": float(d.get("what_if_calls", pd.Series([], dtype=float)).sum()),
            "cand_sum": float(d.get("candidate_count", pd.Series([], dtype=float)).sum()),
            "eval_sum": float(d.get("evaluated_count", pd.Series([], dtype=float)).sum()),
            "osc_avg": float(d.get("oscillation_rate", pd.Series([], dtype=float)).mean()),
            "stability_end": float(d.get("stability_score", pd.Series([], dtype=float)).iloc[-1]) if len(d) else 0.0,
            "csv": run,
        }
        rows.append(row)
    out = pd.DataFrame(rows)
    sort_cols = ["bench", "wtype", "wdcg_on", "lam_policy", "fixed_lam"]
    out = out.sort_values(sort_cols).reset_index(drop=True)
    return out


def plot_curves(df: pd.DataFrame, bench: str, wtype: str, outdir: Path) -> None:
    dd = df[(df["bench"] == bench) & (df["wtype"] == wtype)]
    if dd.empty:
        return

    def label_of(sub: pd.DataFrame) -> str:
        lam = str(sub["lam_policy"].iloc[0])
        w = int(sub["wdcg_on"].iloc[0])
        if lam == "fixed":
            fx = sub["fixed_lam"].iloc[0]
            fx_s = f"{fx}" if fx not in ("", None) else ""
            lam = f"fixed{fx_s}"
        return f"lam={lam}, wdcg={w}"

    def plot_one(metric: str, fname: str) -> None:
        plt.figure()
        for (_, _, lam, fixed_lam, wdcg_on, run), sub in dd.groupby(["bench", "wtype", "lam_policy", "fixed_lam", "wdcg_on", "run"], dropna=False):
            if metric not in sub.columns:
                continue
            sub2 = sub.sort_values("round")
            plt.plot(sub2["round"], sub2[metric], label=label_of(sub2))
        plt.xlabel("round")
        plt.ylabel(metric)
        plt.title(f"{bench} / {wtype} : {metric}")
        plt.legend(fontsize=8)
        outdir.mkdir(parents=True, exist_ok=True)
        plt.tight_layout()
        plt.savefig(outdir / fname, dpi=160)
        plt.close()

    plot_one("total", f"{bench}_{wtype}_total.png")
    plot_one("exec", f"{bench}_{wtype}_exec.png")
    plot_one("rec", f"{bench}_{wtype}_rec.png")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", dest="glob_pat", default=None, help="Glob for CSVs (default: log/adaselect_*.csv)")
    ap.add_argument("--bench", default=None)
    ap.add_argument("--wtype", default=None)
    ap.add_argument("--no_plots", action="store_true")
    args = ap.parse_args()

    pat = args.glob_pat or "log/adaselect_*.csv"
    paths = sorted(glob.glob(pat))
    if not paths:
        print(f"No CSV matched: {pat}")
        return 2

    runs: List[RunId] = []
    for p in paths:
        rid = parse_run_id(p)
        if rid is None:
            continue
        if args.bench and rid.bench != args.bench:
            continue
        if args.wtype and rid.wtype != args.wtype:
            continue
        runs.append(rid)

    if not runs:
        print("No runnable CSVs after filtering.")
        return 2

    dfs = [load_one(r) for r in runs]
    df = pd.concat(dfs, ignore_index=True)

    summ = summarize(df)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 240)
    print(summ.to_string(index=False))

    if not args.no_plots:
        outdir = Path("analysis")
        for (bench, wtype), _ in df.groupby(["bench", "wtype"], dropna=False):
            plot_curves(df, str(bench), str(wtype), outdir)
        print(f"\nSaved plots to: {outdir.resolve()}")

    # Write summary csv too
    out_csv = Path("analysis") / "lambda_wdcg_sweep_summary.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    summ.to_csv(out_csv, index=False)
    print(f"Saved summary CSV to: {out_csv.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
