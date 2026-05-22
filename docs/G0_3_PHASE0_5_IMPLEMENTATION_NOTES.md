# G0-3 + Phase 0.5 implementation notes

This patch advances the 20260408 code base along the agreed plan:

- G0-2 remains the base candidate source (`g0_selection` selection-only structured enumerator).
- G0-3 is implemented as a merge layer, not a new blind candidate source.
- Phase 0.5 hardens the pipeline with:
  - small-table pre-filter
  - high-DML hard skip
  - compile validation as a hard gate after Top-K selection
  - richer per-round and trace observability

## Major code changes

### 1. `adaselect_pp/core/structured_merge.py`
New G0-3 merger.

Implemented:
- same-instance suffix merge only
- `MERGE_GROUP`
- `MERGE_ORDER`
- `MERGE_COVER`
- width bound (`g0_merge_max_width`, default 3)
- conservative `base + 1 suffix` merge only
- table-local cap for merged keys

### 2. `adaselect_pp/core/generator.py`
Pipeline changes:
- after G0-2 enumeration, apply G0-3 merge
- after Top-K selection, apply compile validation hard gate
- optional refill after compile rejection
- high-DML tables are pruned before enumeration
- merge/compile stats are exported in `GenerationResult.stats`

### 3. `database/database_connector.py`
Added best-effort HypoPG metadata helpers:
- `get_virtual_index_oid`
- `get_virtual_index_metadata`

These are used by compile validation to match the actual hypothetical index against `EXPLAIN` plans.

### 4. `adaselect_pp/core/template_extractor.py`
When `sqlglot` recovers `GROUP BY` / `ORDER BY`, the shadow graph now marks them as effective hints with AST confidence.

### 5. `adasel/ada_select.py`
Added config plumbing for:
- G0-3 merge knobs
- Phase 0.5 compile validation knobs

### 6. `util/trace_recorder.py`
Trace now records:
- `lambda_shadow`
- `enum_mode`
- `family`, `base_family`, `merge_family`, `merge_suffix_source`
- `compile_valid`, `compile_pick_reason`, `skip_reason`
- `table_row_count`, `table_dml_ratio`
- `width_before_merge`, `width_after_merge`

### 7. `util/metrics_recorder.py` + `adasel/main.py`
Per-round CSV now records:
- merge counts
- compile validation counts
- skipped high-DML table counts
- selected-after-compile count

## New config

Added `adasel/config/adaselect_g0_3_phase0_5.json`.

Also refreshed:
- `adasel/config/adaselect_g0_selection.json`
- `adasel/config/adaselect.json`

Key defaults:
- `wdcg_graph_ast_backend = sqlglot`
- `wdcg_use_plan = true`
- `g0_merge_enabled = true`
- `phase0_compile_validation = true`

## Tests

Added:
- `adaselect_pp/tests/test_structured_merge.py`

Verified locally:
- `pytest -q adaselect_pp/tests/test_structured_selection.py adaselect_pp/tests/test_structured_merge.py`
- 7 tests passed
