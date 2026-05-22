# Clean AdaSelect++ Spine Rewrite

This package is a deliberate reset. It does not try to keep old G0/Phase1/retain-swap/compile switches alive.

## Active mainline

- `adasel/ada_select.py`: rewritten small AdaSelect class.
- `adaselect_pp/candidates_v1/*`: static SQL evidence candidate generator.
- `database/database_connector.py`: SQL-only guard at DB entrypoints.
- `database/cost_evaluation.py`: SQL-only guard before cost calls.
- `adasel/main.py`: preserves real template id as `<template_id>\t<SQL>` and strips SQL only for execution.

## Removed from active mainline

- CooccurrenceEnumerator
- old WDCG/G0 generator
- structured merge / G0-3 merge
- retain/swap
- compile validation hard gate
- unsafe template/signature EXPLAIN plan cache
- Phase1 U_keep / U_anchor split-visibility code

Legacy source files are archived under `adaselect_pp/archive_legacy/core/` for inspection only.

## Candidate generation first principles

1. Candidate generation uses static SQL evidence, schema, and optional benchmark indexable-column whitelist.
2. EXPLAIN plan is not used to decide which columns exist in the candidate space.
3. Multi-column candidates are bounded B-tree shapes: EQ1, JOIN_EQ1, RANGE1, EQ_EQ, EQ_RANGE.
4. Strong EQ_EQ only uses high-confidence AST same-factor filter equalities; no all-pair explosion.
5. Candidate-vacuum rescue adds at most one viable single-column candidate when a table has evidence but would otherwise have no candidate after PK/UNIQUE filtering.
6. GROUP/ORDER/COVERING are not in the base candidate generator.

## Config

Use:

```bash
cp adasel/config/adaselect_clean_spine.json adasel/config/adaselect.json
```

The sweep script can still override alpha/beta/optimizer_ratio/lambda/wdcg/min_width/max_width.

## Caution

This is not intended to reproduce old G0/ranker-redesign results. It is a clean baseline designed for explainability and controlled future improvement.
