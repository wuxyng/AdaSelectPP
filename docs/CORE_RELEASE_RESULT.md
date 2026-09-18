# Core v2 release outcome: CORE_V2_BLOCKED

## Result

The first live attempt on frozen `94c85dbb0356e5e511fc10ceeab3fbebeb64f182` was invalid and was stopped. Zero workloads completed valid release acceptance. The other eight workloads were not started. No second campaign, 18/108-run matrix, performance retuning or production merge was performed.

The agent-written runtime driver called `_safe_explain_query(..., analyze=True)`, but the accepted function validates SQL and accepts no such keyword. Its TypeError was caught by an overly broad runtime error handler and recorded as the historical 30-second database-error penalty. This triggered repeated timeout resets. Those penalties are not measured execution times, and this trajectory cannot validate the Core policy or performance.

The corrected driver uses the exact historical statement construction, `EXPLAIN (ANALYZE, FORMAT JSON) ` plus validated SQL. Program/connection failures without a PostgreSQL SQLSTATE now stop immediately instead of entering the database-error penalty path. Real PostgreSQL errors retain the original penalty protocol. The patch also records QCP from incomplete runs/rounds instead of reporting only completed workloads. These corrections were pure-tested; live work remains stopped.

## Nine-workload coverage

All nine registered rows are retained; no workload was selectively omitted for performance. Dashes mean no valid release metric is available, not zero time.

| Workload | Valid rounds / planned | Attempt state | Exec / rec / trans / total | Actual QCP / case cap |
|---|---:|---|---|---:|
| tpch_shifting | 0 / 80 | Invalid: 55 rows written, window 56 interrupted | — / — / — / — | 985 / 1402 |
| tpch_noisy | 0 / 96 | Not started after correctness stop | — / — / — / — | 0 / 1631 |
| tpch_random | 0 / 25 | Not started | — / — / — / — | 0 / 1968 |
| tpchs_shifting | 0 / 80 | Not started | — / — / — / — | 0 / 1253 |
| tpchs_noisy | 0 / 96 | Not started | — / — / — / — | 0 / 1522 |
| tpchs_random | 0 / 25 | Not started | — / — / — / — | 0 / 1970 |
| job_shifting | 0 / 80 | Not started | — / — / — / — | 0 / 3515 |
| job_noisy | 0 / 96 | Not started | — / — / — / — | 0 / 4176 |
| job_random | 0 / 25 | Not started | — / — / — / — | 0 / 4834 |

## Resource and restoration evidence

- Preflight: PASS, 15 actual EXPLAINs of the separately allowed 30. Original snapshot/environment/catalog identities matched for all three databases.
- Failed attempt: 985 actual optimizer QCP, including 972 in recorded rows and 13 in the interrupted final window. The original supervisor's completed-only counter was zero; the immutable raw run counter and correction receipt establish the actual total. No uncharged requests or budget increase is claimed.
- Campaign wall: 283.465 seconds. Run wall: 282.443 seconds. Final cleanup: 0.074 seconds, within the 900-second grace period. No active campaign/worker remains.
- Owned DDL: 203 create attempts and 203 drop attempts. Both worker postflight and parent recovery report exact original catalog restoration. Final epoch hash: `4f2221543d6422e622e62392d349dcb8633e6078948e288fc37f3c1d2c8f04c4`.
- Failed-attempt resource components only: optimizer EXPLAIN 0.821 s, scope-exclusive validation 18.553 s, other recommendation 12.735 s; inclusive recommendation 32.109 s; transition/reset/cleanup 250.160 s. These are failure-accounting costs, not a valid workload performance row. The raw 8,400-second execution penalty and resulting total are invalid for performance use.
- Completed invalid rows contain 486 approximate observations, 194 deferrals, zero full width-2 ADD observations, and zero stranded-budget assertions. Frequent invalid resets prevented a meaningful grow trajectory. Therefore live full-ADD/D07 coverage is **not verified**.

## Mechanical evidence and correction scope

Original pre-live revision: 317 tests passed locally and on workstation Python 3.10.20; 490 actual-method ledger observations passed on both hosts. The gap was the runtime EXPLAIN call boundary, which those tests did not exercise.

The corrective patch adds actual runtime-query-builder tests for original SQL, TypeError/connection failure, PostgreSQL timeout, forbidden SQL, malformed runtime values, and incomplete-round accounting: 330 local tests passed, zero skips. Policy, generator, estimator/history, selector and accepted safety transport have no diff from `94c85db`. The 490 measured-state regression remains the measurement evidence; no invented response or live rerun was used to replace the failed attempt.

## Artifacts and commands

- Frozen failed run: `/home/lx/experiments/core_v2_94c85db_20260918`.
- Original workstation tests/replay: `/home/lx/experiments/core_v2_94c85db_mechanical`.
- Local evidence: `E:/AdaSelectPP-core-slimming-v2-results/remote_evidence/`.
- Immutable stopped archive SHA-256: `3b7c9da15dc4b0dbe81a0a0f5b5b62516eb438a58a90e203e4ef294abad69e32`.
- `release_invalidated.json` records the reason, all QCP including the interrupted window, DDL/restoration, and stopped process IDs. Original logs/manifests remain unchanged.

Executed from the frozen workstation checkout:

```text
/home/lx/anaconda3/envs/adaselpp/bin/python -m pytest -q --junitxml=/home/lx/experiments/core_v2_94c85db_mechanical/tests.xml
/home/lx/anaconda3/envs/adaselpp/bin/python experiments/core_v2/replay.py --diagnostic /home/lx/experiments/mask_diagnostic_v2_live_20260917_135639 --package /home/lx/work/AdaSelectPP_width2_mask_fix_v1/contract --output /home/lx/experiments/core_v2_94c85db_mechanical/replay
/home/lx/anaconda3/envs/adaselpp/bin/python experiments/core_v2/prepare.py --previous /home/lx/experiments/width2_mask_fix_v1_97d9259_20260917/registration.json --diagnostic /home/lx/experiments/mask_diagnostic_v2_live_20260917_135639 --output /home/lx/experiments/core_v2_94c85db_20260918
/home/lx/anaconda3/envs/adaselpp/bin/python -m adasel.main --output /home/lx/experiments/core_v2_94c85db_20260918 campaign
```

Corrective pure tests, with no database work:

```text
E:/AdaSelectPP-core-slimming-v2-results/minimal_env/Scripts/python.exe -m pytest -q --junitxml=E:/AdaSelectPP-core-slimming-v2-results/mechanical/postfix_tests.xml
```

## Gates and stop

- Core mechanical/490 fixed-state regression: PASS within its stated coverage.
- Original profile/source and separate safety preflight: PASS.
- Resource accounting and actual restoration of the attempted run: PASS after reconciling the interrupted window; original completed-only summary retained as evidence of the reporting defect.
- Real runtime protocol and nine-workload acceptance: FAIL.
- Overall: **CORE_V2_BLOCKED**.

Only screening was authorized; statistical significance is unproven. This failed attempt does not even provide a valid screening performance result. The corrected revision requires a later explicitly reviewed release attempt; none was automatically started.
