#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_summary.py  —  Generate comparison charts of Exec/Rec/Trans/Total totals
for multiple algorithms over three workloads (shifting / noisy / random).

Fix: aggregate() now prefers the CSV's summary row when present, instead of
summing all rows (which double-counts). If no summary row exists, it will sum
only rows with numeric `round`.

Key improvements (2025‑05‑13):
* Legend and title spacing tightened — legend sits just below the title.
* GridSpec only 2 rows (plots + table).
"""
import os, sys, glob, argparse, pandas as pd
try:
    import matplotlib.pyplot as plt; import matplotlib.gridspec as gridspec
    from matplotlib.lines import Line2D
except ImportError:  # pragma: no cover
    print("Install matplotlib: pip install matplotlib"); sys.exit(1)

WORKLOADS = ['shifting', 'noisy', 'random']
METRICS   = ['exec', 'rec', 'trans']
MN        = {'exec':'Exec','rec':'Rec','trans':'Trans','total':'Total'}

METRICS_MS = ("exec_ms", "rec_ms", "trans_ms")
AMETRICS = ("what_if_calls", "reconf_add", "reconf_drop", "trans_create", "trans_drop")
WORKLOADS = ("shifting", "noisy", "random")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def find_csv(path:str, bm:str, wl:str)->str:
    if os.path.isfile(path): return path
    patt = os.path.join(path, f"*{bm}_{wl}*.csv")
    m = glob.glob(patt)
    if not m: raise FileNotFoundError(patt)
    return m[0]


def aggregate(csvp: str) -> dict:
    """Aggregate totals from a run CSV.

    Rules:
    - If a *summary* row exists (typically the last row where `round` is non-numeric
      or literally equal to "summary"), use that row's exec/rec/trans as totals.
    - Otherwise, sum exec/rec/trans over rows with numeric `round` values only.
    - Convert milliseconds to seconds; compute `total = exec + rec + trans`.
    """
    df = pd.read_csv(csvp)

    totals_ms = None
    if 'round' in df.columns:
        round_num = pd.to_numeric(df['round'], errors='coerce')
        # Prefer explicit summary rows (non-numeric round)
        summary_rows = df[round_num.isna()]
        if not summary_rows.empty:
            s = summary_rows.iloc[-1]
            try:
                totals_ms = {m: float(s[m]) for m in METRICS}
            except Exception:
                # fallback to summing numeric rounds
                totals_ms = {m: float(df[round_num.notna()][m].sum()) for m in METRICS}
        else:
            totals_ms = {m: float(df[round_num.notna()][m].sum()) for m in METRICS}
    else:
        # No 'round' column: best-effort detection of summary row at the end
        if len(df) >= 2:
            sums_prev = {m: float(df.iloc[:-1][m].sum()) for m in METRICS}
            last_vals = {m: float(df.iloc[-1][m]) for m in METRICS}
            if all(abs(sums_prev[m] - last_vals[m]) <= 1e-6 for m in METRICS):
                totals_ms = last_vals
            else:
                totals_ms = sums_prev
        else:
            totals_ms = {m: float(df[m].sum()) for m in METRICS}

    d = {m: totals_ms[m]/1000.0 for m in METRICS}
    d['total'] = sum(d[m] for m in METRICS)
    return d

# -----------------------------------------------------------------------------
# Plot
# -----------------------------------------------------------------------------

def plot_benchmark(bm:str, data:dict, labels:list, outdir:str):
    metrics_all = METRICS+['total']; nmet=len(metrics_all); bw=0.8/len(labels)
    fig = plt.figure(figsize=(16,9))
    gs  = gridspec.GridSpec(2,len(WORKLOADS), height_ratios=[3,1], hspace=0.35)

    # -- bar charts --
    for i, wl in enumerate(WORKLOADS):
        ax=fig.add_subplot(gs[0,i])
        for j,lbl in enumerate(labels):
            vals=[data[lbl][wl][m] for m in metrics_all]
            ax.bar([x+j*bw for x in range(nmet)], vals, width=bw, label=lbl)
        ax.set_xticks([x+(len(labels)-1)*bw/2 for x in range(nmet)])
        ax.set_xticklabels([MN[m] for m in metrics_all])
        ax.set_title(wl.capitalize()); ax.set_ylabel('Time (s)')
        ax.grid(axis='y', ls='--', lw=.5)

    # -- title above legend --
    fig.suptitle(f"{bm.upper()} Summary", fontsize=16, y=0.98)
    fig.legend(labels, loc='upper center', ncol=len(labels), bbox_to_anchor=(0.5, 0.95), frameon=False)

    # -- table --
    ax_tab=fig.add_subplot(gs[1,:]); ax_tab.axis('off')
    col_lbl=[MN[m] for _ in WORKLOADS for m in metrics_all]
    cell=[]
    for lbl in labels:
        row=[]
        for wl in WORKLOADS:
            row.extend(f"{data[lbl][wl][m]:.2f}" for m in metrics_all)
        cell.append(row)
    tbl=ax_tab.table(cellText=cell,rowLabels=labels,colLabels=col_lbl,loc='center',cellLoc='center')
    tbl.auto_set_font_size(False); tbl.set_fontsize(10); tbl.scale(1,1.5)
    # separators & workload headers
    pos=ax_tab.get_position(); left,right=pos.x0,pos.x1; bwid=(right-left)/len(WORKLOADS)
    for k,wl in enumerate(WORKLOADS):
        xc=left+bwid*k+bwid/2
        fig.text(xc,pos.y1+0.01,wl.capitalize(),ha='center',va='bottom',fontsize=12,weight='bold')
        if k<len(WORKLOADS)-1:
            xs=left+bwid*(k+1)
            fig.add_artist(Line2D([xs,xs],[pos.y0,pos.y1], transform=fig.transFigure, color='black', lw=1.5))

    os.makedirs(outdir,exist_ok=True)
    fig.savefig(os.path.join(outdir,f"{bm}_summary.png"),dpi=300,bbox_inches='tight')
    plt.close(fig)

# -----------------------------------------------------------------------------
# Main CLI
# -----------------------------------------------------------------------------

def main():
    pa=argparse.ArgumentParser(description='Generate summary charts')
    pa.add_argument('--dirs',nargs='+',required=True)
    pa.add_argument('--labels',nargs='+',required=True)
    pa.add_argument('--benchmarks',nargs='+',required=True,choices=['tpch','tpchs','job'])
    pa.add_argument('--output_dir',required=True)
    a=pa.parse_args()
    if len(a.dirs)!=len(a.labels): pa.error('dirs/labels mismatch')
    for bm in a.benchmarks:
        d={lbl:{} for lbl in a.labels}
        for lbl,p in zip(a.labels,a.dirs):
            for wl in WORKLOADS:
                d[lbl][wl]=aggregate(find_csv(p,bm,wl))
        plot_benchmark(bm,d,a.labels,a.output_dir)

if __name__=='__main__':
    main()
