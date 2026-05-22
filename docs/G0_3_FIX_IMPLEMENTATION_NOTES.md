# G0-3 fix: no legacy fallback, base-slot protection, compile merge-only/off by default

This patch fixes the two dominant regressions observed in the first G0-3 ablation runs:

1. `group_legacy` / `order_legacy` suffixes were being emitted and selected into the live configuration.
2. merge candidates were competing head-to-head with G0-2 base keys and could crowd them out.
3. compile validation was hard-rejecting useful base candidates; even when enabled, it should never gate base selection keys.

## What changed

### 1) Structured group/order only

`adaselect_pp/core/structured_merge.py`

- `GroupOrderCoveringMerger` now defaults to:
  - `allow_legacy_group_fallback = False`
  - `allow_legacy_order_fallback = False`
  - `allow_legacy_payload_fallback = False`
  - `require_ast_group_order = True`
- `group_legacy` / `order_legacy` are no longer emitted unless explicitly re-enabled.
- group/order suffixes require structured evidence:
  - `*_effective_hint == True`, or
  - `QualityMark.source == 'AST'` and confidence above threshold.
- plan-quality / query-quality alone no longer unlock group/order merges.

### 2) Base-slot reservation and merge cap

`adaselect_pp/core/generator.py`

- `TopKSelector` now supports:
  - `reserve_base_count`
  - `max_merge_count`
- selection order is changed to:
  - seeds
  - width-2 base keys
  - reserve base slots
  - remaining base keys
  - merge keys
- rescue / eviction prefers evicting merges first when base reservation is under target.
- stats now expose:
  - `selected_base_count`
  - `selected_merge_count`
  - `reserved_base_target`

### 3) Compile validation no longer gates base keys

`adaselect_pp/core/generator.py`

- compile validation now supports `merge_only=True`.
- when `merge_only=True`, base keys are marked as `compile_skipped_base` and are never hard-rejected.
- compile validation remains optional and is OFF by default in the fixed mainline config.
- refill is also OFF by default.

### 4) AdaSelect defaults / config plumbing

`adasel/ada_select.py`

Added config knobs:

- `g0_merge_allow_legacy_group_fallback`
- `g0_merge_allow_legacy_order_fallback`
- `g0_merge_allow_legacy_payload_fallback`
- `g0_merge_require_ast_group_order`
- `g0_merge_reserve_base`
- `g0_merge_max_selected`
- `phase0_compile_merge_only`

Mainline defaults now favor safe G0-3 behavior:

- no legacy fallback
- reserve 7 base slots
- cap merges to 3
- compile validation OFF by default

## Recommended configs

- `adasel/config/adaselect_g0_3_fixed_mainline.json`
  - mainline G0-3
  - compile OFF
  - no legacy fallback
  - base-slot protection enabled

- `adasel/config/adaselect_g0_3_compile_merge_only_diag.json`
  - diagnostic only
  - compile ON for merge candidates only
  - no refill

## Validation

Executed locally:

- `pytest -q adaselect_pp/tests/test_structured_selection.py adaselect_pp/tests/test_structured_merge.py`
- Result: `10 passed`

Added tests:

- no legacy group/order fallback by default
- base-slot reservation + merge cap
- compile merge-only skips base keys but still checks merges
