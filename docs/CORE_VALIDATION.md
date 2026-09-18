# Core v2 validation before live registration

These are new executions, not inherited historical scores.

## Local mechanical results

- 317 active tests passed; zero skipped. Minimal Python 3.12 environment has sqlglot 30.8.0 and pytest 9.0.3, with no numpy, torch, TCNN or installed psycopg2. The database driver imports without opening a connection.
- Actual new `_test_candidate`: 490 measured fixtures passed; 237 width-2 FULL_WINDOW_ADD, 253 width-1 LEGACY_MASK_APPROX; 73 targeted nonpositive-mask/positive-full recoveries. Reference M2Q history and call sequences match in all 490 cases. Maximum absolute raw difference from serialized expected values is 2.0980951376259327e-07, within the registered `atol=1e-8, rtol=1e-10` test.
- Exact profile ancestry passed against the prior accepted registration: 9 cases, 603 original physical-window caps, sum 22,271; same traces/order/alpha/beta/max_num/timeouts/constants.
- Q1: root 2 + six completed targets × 2 = 14; the final two are deferred. The old two-candidate boundary does not limit dispatch.
- Q2: a 21-QCP full target is deferred; later requirements 3 and 4 both fit in the remaining 8.
- Q3/Q4: first-round root 5 + target 5 uses B=10; B=4 or 0 holds with no baseline fabrication.
- Q5/Q6: structural promotion is independent of the obsolete m>=2 rule; binding/reset of the same physical window preserves seven paid requests out of ten, while the next window starts its own cap.
- Q7/Q8: partial full failure remains charged, restores target, never updates history; zero-request NO_HIT runs once even at zero balance and is not counted FULL.
- Q9–Q12: ADD/KEEP/mask/occurrence/identity/fallback boundaries, strict config/CLI/environment rejection, shared real-EXPLAIN guard, fixed-state generator/history/selector reference outputs and observer isolation are tested.
- 178 historical JUnit cases individually classified: 57 ported assertions, 25 replacement tests for deliberately changed interfaces/dispatch, 96 retired obsolete experiment/reporting cases. See `CORE_TEST_MIGRATION.json` for every original parameterized ID and rationale. Historical 178 passes are not claimed as current passes.

## Executed local commands

Working directory: `E:/AdaSelectPP-core-v2`.

```text
E:/AdaSelectPP-core-slimming-v2-results/minimal_env/Scripts/python.exe -m pytest -q --junitxml=E:/AdaSelectPP-core-slimming-v2-results/mechanical/minimal_tests.xml
E:/AdaSelectPP-core-slimming-v2-results/minimal_env/Scripts/python.exe experiments/core_v2/replay.py --diagnostic E:/AdaSelectPP/runs/width2_mask_fix_v1_support/fixtures --package E:/AdaSelectPP/runs/width2_mask_fix_v1_input --output E:/AdaSelectPP-core-slimming-v2-results/mechanical/replay_final
```

The packet's own `validate_package.py` also passed (HANDOFF_FILES_VALID); it is an input check, not a production test. `prepare.verify_profiles` was executed against `E:/AdaSelectPP/runs/width2_mask_fix_v1_support/registration.json`. `git diff --check` and Python syntax parsing passed.

## Live evidence boundary

No intermediate dual-budget matrix was run. The final clean revision must pass the same pure suite and replay on the workstation, then register once using `prepare.py`. Only that version receives the nine-workload campaign. The external `run_manifest.json` records exact commit/file hashes, actual commands, preflight and completion; `RELEASE_REPORT.md` / `release_report.json` contain actual results. This pre-freeze document does not claim live completion. New responses or trajectories are not fabricated from the historical ledger.
