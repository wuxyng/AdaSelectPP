# G0 Single-Spine Cleanup

This cleanup removes the old mixed-mode behavior from the WDCG path and forces a single G0 mainline.

## What changed

- WDCG candidate generation is now **always** `g0_selection`.
- Legacy co-occurrence generation is no longer reachable from the WDCG path.
- Compile validation is removed from the main path.
- Legacy group/order/payload fallback is not passed into the merger.
- AST is now a **hard requirement** for the G0 path:
  - missing backend => runtime error
  - parse failure => runtime error
- Structured selection no longer falls back to legacy conservative generation.
  - missing graph / missing structured instances => runtime error
  - low-quality graph stays graph-sparse, but still graph-only
- `adaselect.json` is replaced by a minimal `G0` mainline config.
- Added `adaselect_g0_selection_only.json` for direct comparison against the same code base.

## Mainline configs

- `adasel/config/adaselect_g0_mainline.json`
- `adasel/config/adaselect_g0_selection_only.json`

## Intent

The goal is to make experiments either:
- run on a real G0 path, or
- fail immediately.

The code no longer silently pretends to run G0 while actually degrading to legacy/fallback behavior.
