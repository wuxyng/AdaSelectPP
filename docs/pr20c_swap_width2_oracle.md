# PR20c Swap Width-2 Oracle

PR20c is an offline what-if oracle tool. It is not an AdaSelectPP online policy
change, and it is not final selector motivation by itself.

The diagnostic asks whether JOB has useful width-2 candidates in the exported
appearing pool. It compares:

- the baseline AdaSelectPP-selected configuration,
- an additive forced width-2 configuration when capacity allows it, and
- an atomic swap configuration that replaces a selected width-1 prefix with the
  corresponding width-2 candidate.

The swap rule prioritizes the primary prefix `(T, c1)` for a width-2 candidate
`(T, c1, c2)`. It only falls back to `(T, c2)` when `(T, c1)` is not selected.

All evaluations use the existing HypoPG/what-if workload cost path. The tool
resets virtual indexes before and after each baseline/add/swap evaluation, and
it does not create physical indexes.

## Interpretation Boundary

PR20c shows what-if replacement value. A positive swap oracle result means that
the HypoPG cost model finds a lower-cost configuration when a selected width-1
prefix is replaced by a width-2 candidate.

This is diagnostic evidence only. It does not prove that the same swap improves
real execution time, nor does it justify selector changes without another
validation step.

PR20d is required before selector changes. PR20d should validate whether the
same replacement candidates improve real execution value under controlled runs.
Only after that real-execution validation should PR21 consider selector-level
retain/swap policy changes.

## Guardrails

PR20c must remain offline-only:

- no AdaSelectPP online policy changes,
- no candidate generation changes,
- no evaluation budget formula changes,
- no optimizer-ratio changes,
- no selector or materialization policy changes,
- no LiteSelect two-CELF/RPA import,
- no physical index materialization.

