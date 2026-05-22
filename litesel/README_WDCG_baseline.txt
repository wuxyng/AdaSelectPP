LiteSelect (baseline) + Phase 0.5 WDCG option
===========================================

Files included
- lite_select_mc_topk.py  (patched version with WDCG pruning/order)
- main.py                 (patched CLI flags & cfg wiring)
- wdcg_patch.diff         (unified diff against your uploaded originals)

How to use
1) Replace your existing files with the patched ones.
2) Run with --wdcg to enable Phase 0.5 pruning/order:
   python main.py --algo liteselect_mc_topk --wdcg

Optional knobs:
   --wdcg_topk 1000
   --wdcg_family_cap 2
   --wdcg_min_table_ratio 0.05
   --wdcg_no_small_table_prune

Behavior summary
- When --wdcg is enabled, LiteSelectMC ranks candidates using plan-derived role columns:
  join_eq / filter_eq / filter_rng / group_by / order_by
- It then keeps top-K after a per-(table, first-col) family cap, ensuring at least one
  candidate per table.
- Budget allocation and evaluation order follow the pruned list.
- Benefits for non-evaluated candidates (including pruned ones) are decayed by alpha
  to avoid stale dominance.

Notes
- This is plan-first: it calls EXPLAIN (FORMAT JSON, VERBOSE) only when WDCG is enabled.
- It uses a plan cache keyed by a SQL signature with literals stripped.
