#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tri-step falsifiable diagnosis for TPC-H random experiments.

Inputs:
  - One or more per_round CSVs (AdaSelect / LiteSelect etc), OR
  - A ZIP that contains those CSVs.

Outputs:
  - Prints a concise report to stdout.
  - Optionally writes a markdown report and some CSV summaries to --out_dir.

This script focuses on the "3-step falsifiable diagnosis":
  (1) aff*: does the chosen/evaluated set have enough affinity/coverage?
  (2) whatif-eval ratio: are we spending evaluation budget efficiently?
  (3) candidate semantic consistency: are configs stable / structurally sane?
"""

from __future__ import annotations
import argparse
import ast
import io
import os
import re
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set

import pandas as pd

IndexKey = Tuple[str, Tuple[str, ...]]


TPCH_JOIN_KEYS = {
    ("orders", "o_orderkey"),
    ("orders", "o_custkey"),
    ("lineitem", "l_orderkey"),
    ("lineitem", "l_partkey"),
    ("lineitem", "l_suppkey"),
    ("part", "p_partkey"),
    ("supplier", "s_suppkey"),
    ("customer", "c_custkey"),
    ("partsupp", "ps_partkey"),
    ("partsupp", "ps_suppkey"),
    ("nation", "n_nationkey"),
    ("nation", "n_regionkey"),
    ("region", "r_regionkey"),
}


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def _parse_conf_cell(x) -> Set[IndexKey]:
    """Parse a config cell in per_round.csv into a set of IndexKey."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return set()
    if isinstance(x, (list, tuple, set)):
        raw = x
    else:
        s = str(x).strip()
        if not s:
            return set()
        try:
            raw = ast.literal_eval(s)
        except Exception:
            # Some logs may store as "tbl(col1,col2); tbl2(col)"
            # Try a very simple fallback parser.
            raw = []
            for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*([^)]+?)\s*\)", s):
                tbl = m.group(1)
                cols = tuple(c.strip().strip('"') for c in m.group(2).split(",") if c.strip())
                if cols:
                    raw.append((tbl, cols))

    out: Set[IndexKey] = set()
    if isinstance(raw, dict):
        it = raw.items()
    else:
        it = raw

    for item in it:
        try:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (tuple, list)):
                tbl = str(item[0])
                cols = tuple(str(c) for c in item[1])
                out.add((tbl, cols))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                tbl = str(item[0])
                cols = tuple(str(c) for c in item[1:])
                out.add((tbl, cols))
        except Exception:
            continue
    return out


@dataclass
class RunData:
    label: str
    df: pd.DataFrame
    source: str  # file path inside zip or on disk


def _guess_label(path: str) -> str:
    base = os.path.basename(path)
    if base.lower().endswith(".csv"):
        base = base[:-4]
    # Keep it readable
    base = base.replace(" ", "_")
    return base


def _load_csvs_from_zip(zpath: str) -> List[RunData]:
    runs: List[RunData] = []
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if not name.lower().endswith(".csv"):
                continue
            # Heuristic: per_round style files often contain "tpch" + "random" or "per_round" patterns.
            low = name.lower()
            if ("tpch" in low and "random" in low) or ("per_round" in low) or ("tpch_random" in low):
                with z.open(name) as f:
                    df = pd.read_csv(f)
                runs.append(RunData(label=_guess_label(name), df=df, source=f"{zpath}:{name}"))
    return runs


def _load_csv(path: str) -> RunData:
    df = pd.read_csv(path)
    return RunData(label=_guess_label(path), df=df, source=path)


def _find_metric_cols(df: pd.DataFrame) -> Dict[str, str]:
    """Map canonical names to actual column names if they differ."""
    cols = {c.lower(): c for c in df.columns}
    def pick(*names):
        for n in names:
            if n.lower() in cols:
                return cols[n.lower()]
        return None

    return {
        "round": pick("round", "rid", "iter"),
        "exec": pick("exec", "exec_ms", "exec_time", "exec_s"),
        "rec": pick("rec", "rec_ms", "recommend", "recommend_ms"),
        "trans": pick("trans", "trans_ms", "transition", "transition_ms"),
        "total": pick("total", "total_ms", "sum", "sum_ms"),
        "timeout": pick("timeout", "is_timeout"),
        "candidate_count": pick("candidate_count", "cand_cnt", "num_candidates"),
        "evaluated_count": pick("evaluated_count", "eval_cnt", "num_evaluated"),
        "what_if_calls": pick("what_if_calls", "whatif_calls", "whatif_cnt"),
        "old_conf": pick("old", "old_conf", "old_config"),
        "new_conf": pick("new", "new_conf", "new_config"),
        "switch_size": pick("switch_size", "sw_size"),
        "switched": pick("switched", "is_switched"),
        "oscillation_rate": pick("oscillation_rate", "osc_rate"),
        "stability_score": pick("stability_score", "stab_score"),
    }


def _summary_table(runs: List[RunData]) -> pd.DataFrame:
    rows = []
    for r in runs:
        df = r.df.copy()
        m = _find_metric_cols(df)
        n = len(df)
        timeout_col = m["timeout"]
        exec_col = m["exec"]
        rec_col = m["rec"]
        trans_col = m["trans"]
        total_col = m["total"]

        timeout_rate = df[timeout_col].mean() if timeout_col and timeout_col in df else 0.0
        def stat(col):
            if col and col in df:
                s = pd.to_numeric(df[col], errors="coerce")
                return float(s.mean()), float(s.median()), float(s.quantile(0.95))
            return (0.0, 0.0, 0.0)

        exec_mean, exec_p50, exec_p95 = stat(exec_col)
        rec_mean, rec_p50, rec_p95 = stat(rec_col)
        trans_mean, trans_p50, trans_p95 = stat(trans_col)
        total_mean, total_p50, total_p95 = stat(total_col)

        cand = df[m["candidate_count"]].sum() if m["candidate_count"] in df else 0.0
        evald = df[m["evaluated_count"]].sum() if m["evaluated_count"] in df else 0.0
        whatif = df[m["what_if_calls"]].sum() if m["what_if_calls"] in df else 0.0

        eval_per_cand = float(evald / cand) if cand else 0.0
        whatif_per_eval = float(whatif / evald) if evald else 0.0
        rec_per_whatif = float(df[m["rec"]].sum() / whatif) if (m["rec"] in df and whatif) else 0.0

        rows.append({
            "label": r.label,
            "rounds": n,
            "timeout_rate": timeout_rate,
            "exec_mean": exec_mean, "exec_p50": exec_p50, "exec_p95": exec_p95,
            "rec_mean": rec_mean, "trans_mean": trans_mean, "total_mean": total_mean,
            "eval/cand": eval_per_cand,
            "whatif/eval": whatif_per_eval,
            "rec/whatif": rec_per_whatif,
            "source": r.source,
        })
    return pd.DataFrame(rows).sort_values("exec_mean")


def _aff_step(df: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in df.columns if c.startswith("aff_")]
    if not cols:
        return pd.DataFrame()
    out = df[cols].apply(pd.to_numeric, errors="coerce")
    return pd.DataFrame({
        "mean": out.mean(),
        "p50": out.median(),
        "p95": out.quantile(0.95),
        "non_null": out.notna().sum(),
    }).sort_values("mean", ascending=False)


def _semantic_step(df: pd.DataFrame) -> Dict[str, object]:
    m = _find_metric_cols(df)
    old_c = m["old_conf"]
    new_c = m["new_conf"]
    timeout_c = m["timeout"]
    exec_c = m["exec"]

    if not new_c or new_c not in df:
        return {}

    new_sets = df[new_c].apply(_parse_conf_cell)
    old_sets = df[old_c].apply(_parse_conf_cell) if (old_c and old_c in df) else pd.Series([set()] * len(df))

    # churn
    churn = []
    w1_frac = []
    w2_frac = []
    join_w1_frac = []
    conf_size = []
    for ns, os_ in zip(new_sets, old_sets):
        conf_size.append(len(ns))
        churn.append(len(ns.symmetric_difference(os_)))
        if ns:
            w1 = sum(1 for _, cols in ns if len(cols) == 1)
            w2 = sum(1 for _, cols in ns if len(cols) == 2)
            w1_frac.append(w1 / len(ns))
            w2_frac.append(w2 / len(ns))
            jw1 = sum(1 for tbl, cols in ns if len(cols) == 1 and (tbl, cols[0]) in TPCH_JOIN_KEYS)
            join_w1_frac.append(jw1 / len(ns))
        else:
            w1_frac.append(0.0); w2_frac.append(0.0); join_w1_frac.append(0.0)

    out: Dict[str, object] = {
        "avg_conf_size": float(pd.Series(conf_size).mean()),
        "avg_churn": float(pd.Series(churn).mean()),
        "avg_w1_frac": float(pd.Series(w1_frac).mean()),
        "avg_w2_frac": float(pd.Series(w2_frac).mean()),
        "avg_join_w1_frac": float(pd.Series(join_w1_frac).mean()),
    }

    # timeout culprit columns/indexes: which indexes appear most often during timeout rounds
    if timeout_c and timeout_c in df:
        tmask = df[timeout_c].astype(bool)
        idx_counter: Dict[IndexKey, int] = {}
        col_counter: Dict[Tuple[str, str], int] = {}
        for s in new_sets[tmask]:
            for tbl, cols in s:
                idx_counter[(tbl, cols)] = idx_counter.get((tbl, cols), 0) + 1
                for c in cols:
                    col_counter[(tbl, c)] = col_counter.get((tbl, c), 0) + 1
        top_idx = sorted(idx_counter.items(), key=lambda kv: kv[1], reverse=True)[:20]
        top_col = sorted(col_counter.items(), key=lambda kv: kv[1], reverse=True)[:20]
        out["timeout_rounds"] = int(tmask.sum())
        out["timeout_top_indexes"] = top_idx
        out["timeout_top_columns"] = top_col

    # exec hot rounds: top-10 exec rounds -> which columns appear
    if exec_c and exec_c in df:
        exec_s = pd.to_numeric(df[exec_c], errors="coerce").fillna(0.0)
        hot = exec_s.sort_values(ascending=False).head(10).index
        idx_counter: Dict[IndexKey, int] = {}
        for s in new_sets.loc[hot]:
            for tbl, cols in s:
                idx_counter[(tbl, cols)] = idx_counter.get((tbl, cols), 0) + 1
        out["hot_exec_top_indexes"] = sorted(idx_counter.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True,
                    help="CSV or ZIP paths. You can pass multiple.")
    ap.add_argument("--out_dir", default="", help="Optional output dir for markdown/csv.")
    args = ap.parse_args()

    runs: List[RunData] = []
    for p in args.inputs:
        if p.lower().endswith(".zip"):
            runs.extend(_load_csvs_from_zip(p))
        else:
            runs.append(_load_csv(p))

    if not runs:
        raise SystemExit("No CSVs found. Provide per_round CSVs or a ZIP containing them.")

    # --- Summary ---
    summ = _summary_table(runs)

    print("\n=== Overall Summary (sorted by exec_mean) ===")
    print(summ.to_string(index=False))

    # --- Tri-step details per run ---
    for r in runs:
        print("\n" + "=" * 90)
        print(f"RUN: {r.label}")
        print(f"SOURCE: {r.source}")
        df = r.df

        # Step 1: aff
        aff = _aff_step(df)
        if not aff.empty:
            print("\n[Step 1] aff* statistics (mean/p50/p95):")
            print(aff.to_string())
        else:
            print("\n[Step 1] aff*: not found in this CSV (no aff_* columns).")

        # Step 2: whatif-eval ratio
        m = _find_metric_cols(df)
        cand = df[m["candidate_count"]].sum() if m["candidate_count"] in df else 0.0
        evald = df[m["evaluated_count"]].sum() if m["evaluated_count"] in df else 0.0
        whatif = df[m["what_if_calls"]].sum() if m["what_if_calls"] in df else 0.0
        rec_sum = df[m["rec"]].sum() if m["rec"] in df else 0.0

        print("\n[Step 2] whatif/eval efficiency:")
        print(f"  candidate_sum = {cand:.0f}")
        print(f"  evaluated_sum = {evald:.0f}  (eval/cand={evald/cand:.3f} if cand>0)")
        print(f"  whatif_sum    = {whatif:.0f} (whatif/eval={whatif/evald:.3f} if eval>0)")
        print(f"  rec_sum       = {rec_sum:.3f} (rec/whatif={rec_sum/whatif:.6f} if whatif>0)")

        # Step 3: semantic consistency
        sem = _semantic_step(df)
        if sem:
            print("\n[Step 3] candidate semantic consistency (from configs):")
            for k in ("avg_conf_size", "avg_churn", "avg_w1_frac", "avg_w2_frac", "avg_join_w1_frac"):
                if k in sem:
                    print(f"  {k}: {sem[k]:.3f}")
            if "timeout_rounds" in sem and sem["timeout_rounds"] > 0:
                print(f"  timeout_rounds: {sem['timeout_rounds']}")
                print("  timeout_top_columns (table,col -> count):")
                for (tbl, col), cnt in sem.get("timeout_top_columns", [])[:10]:
                    print(f"    {tbl}.{col}: {cnt}")
                print("  timeout_top_indexes (tbl,cols -> count):")
                for (tbl, cols), cnt in sem.get("timeout_top_indexes", [])[:10]:
                    print(f"    {tbl}{cols}: {cnt}")
            print("  hot_exec_top_indexes (in top-10 exec rounds):")
            for (tbl, cols), cnt in sem.get("hot_exec_top_indexes", [])[:10]:
                print(f"    {tbl}{cols}: {cnt}")
        else:
            print("\n[Step 3] semantic: cannot parse configs (missing old/new columns).")

    # Optional outputs
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        summ.to_csv(os.path.join(args.out_dir, "summary.csv"), index=False)
        # Also write one markdown file
        md_path = os.path.join(args.out_dir, "tri_step_report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# Tri-step Diagnosis Report (TPC-H random)\n\n")
            f.write("## Overall Summary\n\n")
            f.write(summ.to_markdown(index=False))
            f.write("\n")
        print(f"\nWrote: {md_path}")
        print(f"Wrote: {os.path.join(args.out_dir, 'summary.csv')}")


if __name__ == "__main__":
    main()
