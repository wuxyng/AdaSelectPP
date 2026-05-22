# G0 Phase 1 Implementation Notes

This package implements the first clean G0 base spine:

- `U_curr` from `StructuredSelectionEnumerator`
- `U_keep` from explicit `InstalledIndexState`
- no legacy recall patch
- no conservative legacy fallback
- no merge on the mainline
- no compile validation on the mainline

## New files
- `adaselect_pp/core/types.py`
- `adaselect_pp/core/base_universe.py`
- `adaselect_pp/core/base_ranker.py`
- `adaselect_pp/core/base_selector.py`

## Mainline wiring
- `adaselect_pp/core/generator.py` now runs only:
  `StructuredSelectionEnumerator -> BaseUniverseBuilder -> StructuredBaseRanker -> SourceAwareBaseSelector`
- `adasel/ada_select.py` maintains `installed_index_state` and passes it to the generator.
- `_choose_config` is restricted to the visible action space: `appearing ∪ old_conf`.
- keep-only incumbents with zero current support are not force-updated through a fake `NO_HIT` path.

## Purged from the mainline
- `_augment_recall`
- compile validation
- merge ranking/selection
- legacy candidate generation
- `SEL_FALLBACK`
- conservative legacy fallback path

## Phase 1 configs
- `adasel/config/adaselect_g0_phase1_mainline.json`
- `adasel/config/adaselect_g0_phase1_curr_only.json`

## Run helper
- `scripts/run_g0_phase1_ablation.sh`
