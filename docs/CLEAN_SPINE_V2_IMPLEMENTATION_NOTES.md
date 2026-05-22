# Clean AdaSelect++ Spine v2

This package rewrites the active AdaSelect++ path around a predicate-first, prefix-growth multi-column candidate generator.

## Active mainline

```text
adasel/main.py
adasel/ada_select.py
adaselect_pp/common/*
adaselect_pp/candidate_gen_v2/*
database/database_connector.py
database/cost_evaluation.py
```

The active loop is intentionally small:

```text
_generate_and_merge_candidates()
-> _estimate_benefits()
-> _choose_config()
-> run()
```

## Removed from active mainline

The active code no longer imports or instantiates:

```text
CooccurrenceEnumerator
StructuredSelectionEnumerator
query-graph fallback regime
G0-3 merge
Phase1 U_keep/U_anchor split visibility
retain/swap
compile validation hard gate
unsafe template/signature EXPLAIN plan cache
```

## Candidate generation rules

Candidate generation uses only static SQL evidence, schema, PK/UNIQUE metadata, and optional benchmark indexable-column whitelist.

It does not call EXPLAIN and does not depend on current installed indexes.

Generated candidate families:

```text
EQ1
JOIN_EQ1
RANGE1
EQ_EQ
EQ_RANGE
VACUUM_RESCUE1
```

No legacy co-occurrence families are generated.

## Prefix growth

- single-column seeds first;
- EQ_EQ only for strong AST same-factor filter equalities, top-2 only;
- EQ_RANGE = best equality-like column + best range column;
- no all-pair enumeration;
- no reverse permutations;
- width <= 2 by default.

## Fallback and rescue

Fallback is bounded static evidence, not legacy co-occurrence.

Candidate-vacuum rescue only fires when a table has SQL evidence but no viable candidate after PK/UNIQUE filtering. It emits at most one single-column candidate.

## Indexable columns

`indexable_columns.txt` is used only as a whitelist/vocabulary:

```text
candidate = SQL-active evidence ∩ whitelist
```

The file is never used to enumerate all column combinations.

## Template id and SQL transport

`load_workloads()` preserves true template ids in canonical `<template_id>\t<SQL>` form. All database entry points strip the template id before sending SQL to PostgreSQL.

## Plan cache

No EXPLAIN plan cache is used. Template/signature plan caching is intentionally absent.

## Configuration

Use:

```text
adasel/config/adaselect_clean_spine.json
```

The file contains only minimal knobs. Old G0/Phase1/compile/retain-swap knobs are ignored.
