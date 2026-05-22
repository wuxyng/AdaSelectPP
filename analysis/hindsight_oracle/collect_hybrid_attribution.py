#!/usr/bin/env python3
"""Collect Hybrid-G0 fallback/refresh attribution metrics from per-round CSV files."""
from __future__ import annotations
import argparse, csv
from pathlib import Path

def f(x):
    try:
        if x in (None,''): return 0.0
        return float(x)
    except Exception:
        return 0.0

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--runs-root', required=True)
    ap.add_argument('--out', required=True)
    args=ap.parse_args()
    root=Path(args.runs_root)
    rows=[]
    for p in root.rglob('*.csv'):
        if p.name.endswith('.trace.csv') or 'summary' in p.name.lower():
            continue
        parts=p.parts
        try:
            idx=parts.index(root.name); variant=parts[idx+1]; case=parts[idx+2]
        except Exception:
            variant=p.parent.parent.name; case=p.parent.name
        with p.open(newline='',encoding='utf-8') as fh:
            rdr=csv.DictReader(fh)
            for r in rdr:
                if str(r.get('round','')).upper()=='SUMMARY': continue
                rows.append((variant,case,r))
    keys=['candidate_count','evaluated_count','what_if_calls','fallback_candidate_count','fallback_selected_count','legacy_supplement_candidate_count','legacy_supplement_selected_count','oldconf_refresh_added','oldconf_refresh_queries','oldconf_refresh_unique_indexes','oldconf_refresh_max_aff','graph_rich_query_count','graph_sparse_query_count','role_fallback_query_count','aff_avg','aff_max','predicted_what_if_calls','total','exec','rec','trans']
    agg={}
    cnt={}
    for variant,case,r in rows:
        k=(variant,case); cnt[k]=cnt.get(k,0)+1
        d=agg.setdefault(k,{kk:0.0 for kk in keys})
        for kk in keys: d[kk]+=f(r.get(kk))
    out=Path(args.out); out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('w',newline='',encoding='utf-8') as fh:
        w=csv.writer(fh); w.writerow(['variant','case','rounds']+[kk+'_sum' for kk in keys]+[kk+'_avg' for kk in keys])
        for k in sorted(agg):
            n=max(1,cnt[k]); vals=agg[k]
            w.writerow([k[0],k[1],cnt[k]]+[vals[kk] for kk in keys]+[vals[kk]/n for kk in keys])
if __name__=='__main__': main()
