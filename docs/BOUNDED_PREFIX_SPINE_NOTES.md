# Bounded Prefix-Growth Spine

This package replaces the erroneous LiteSelect exhaustive-permutation candidate generation with a bounded predicate-first prefix-growth generator.

Active candidate generation:
- static SQL evidence only;
- single-column predicate/join/range seeds;
- bounded width-2 prefix growth;
- per-query/per-table/round-table caps;
- PK/UNIQUE filtering;
- candidate-vacuum rescue only as single-column fallback.

Borrowed from LiteSelect only:
- detailed logger messages;
- timeout reset discipline;
- budgeted what-if benefit estimation;
- topk-beta transition decision.

Not active:
- exhaustive permutations;
- CooccurrenceEnumerator;
- G0-3 merge;
- compile validation hard gate;
- retain/swap;
- EXPLAIN-plan-derived candidate generation.
