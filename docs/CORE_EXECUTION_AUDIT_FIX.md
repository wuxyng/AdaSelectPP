# Core v2 execution audit fixes

Base: `a2d70bfe36b5c45ca1c519c715f5e6051de6ca0e`. Existing draft PR #43.

Status: **implementation submitted for independent re-verification; release remains BLOCKED**.
No database connection, live preflight, workload acceptance or parameter tuning is authorized by this fix.

## Scope and error semantics

Only execution, resource receipts, supervision, offline reporting and their tests change.
Core candidate generation, scheduling, estimator/history, selector, policy constants,
registered profiles, QCP caps, timeout values and accepted HypoPG/DDL transport are unchanged.

`database.execution_errors.classify_error` is shared by execution and reporting.
Only SQLSTATE `57014` identified as a **statement timeout** retains the historical
30,000 ms protocol penalty. User cancellation, unrecognized errors, SQL source errors,
permission/setup failures, connection loss, server shutdown, resource exhaustion and
internal failures stop. Fatal optimizer transport errors also escape the controller as
`CampaignStop`; they cannot become ordinary failed measurements followed by further tuning.
Offline reading applies the same classification to old events, including old COMPLETE artifacts.

`exec_observed_s` contains returned `Plan.Actual Total Time` values only.
`exec_protocol_penalty_s` contains historical timeout penalties only; `exec_s` remains
their sum for protocol compatibility. `exec_wall_s` includes failed/partial occurrences.
The native reset receives the same 30,000 ms input only for an identified statement timeout.
Fatal failures never contribute a fabricated runtime or penalty.

## Resource receipts and uncertainty

Each completed occurrence updates known execution cost immediately. Execution and transition
wall time accumulate in `finally`, including failed transitions and native-reset DDL.
Cleanup, final validation and every acquired connection's close are attempted independently.
The primary error survives secondary failures; all secondary errors remain in receipts.
Parent restoration time is recorded separately, including recovery failure.

Atomic, flushed receipts record QCP intent before dispatch and response after completion.
`qcp_used` is budget charges; `qcp_actual_attempts` is null if a local interruption makes
the actual dispatch count uncertain. `qcp_confirmed_lower_bound` and in-flight intent are
separate evidence. Runtime request intents and confirmed responses are also distinct.
An unfinalized/missing worker receipt has unknown actual QCP and is conservatively charged
the entire registered workload cap. This does not spend or extend its allowed cap.
Preflight has its own corresponding receipt and conservative 30-request upper charge.
Unfinalized timing is explicitly incomplete, with known lower bounds and no complete total.
Round counters cover completed CSV rows; unfinished work is not claimed as release coverage.

## Process order and preflight

The supervisor registers spawn intent first. Parent interruption/deadline failure causes
TERM, bounded wait, KILL if needed, and another bounded wait. Recovery runs only after
confirmed child exit. A failed exit confirmation prevents recovery and subsequent workloads.
Signals during process creation are deferred until the child handle is owned; repeated
signals are suppressed during stop/reap and parent recovery. Original failure and recovery
failure are both retained.

Preflight now calls the real `execute_occurrence()` before changing the connection to the
measurement transaction mode. Three runtime checks plus the original 15 optimizer checks
share the existing 30-EXPLAIN cap. These checks were exercised with fake connections only.

## Validation and reproducer provenance

The independent auditor's original reproducer SHA-256 is
`63578184dd672086df037cc026a55101bcdc270aeb01f9b925ca1fd0a5fdfc45`.
`tests/core/audit_execution_support.py` adapts its real
`main -> run_one -> execute_occurrence -> AdaSelect -> report` harness.
Its fixtures substitute database/process boundaries, not policy decisions.

The **same 24 acceptance assertions fail on the archived base and pass on this fix**.
Windows Python 3.12: **360 passed**. WSL Ubuntu Python 3.14: **28 passed**,
including real POSIX process-group TERM/KILL/reap checks. No tests were skipped.
There are no xfails or skips masking the old defects. Additional report and real OS child
tests cover missing receipts, in-flight uncertainty, old fatal SQLSTATE artifacts and
termination ordering. Real children execute only a readiness marker and sleep; no database
driver is loaded. POSIX tests verify an ignored TERM requires SIGKILL and is reaped first.

Commands executed from `E:/AdaSelectPP-core-v2` (evidence root abbreviated as `$evidence`):

```powershell
$python = 'E:/AdaSelectPP-core-slimming-v2-results/minimal_env/Scripts/python.exe'
$evidence = 'E:/AdaSelectPP-core-slimming-v2-results/execution_audit_fix'
& $python -B -m pytest tests/core -q -p no:cacheprovider --junitxml="$evidence/final_verified_tests.xml" --basetemp="$evidence/final_verified_tmp"
$env:CORE_AUDIT_SOURCE = "$evidence/baseline_a2d70bf"
& $python -B -m pytest tests/core/test_execution_audit.py -q -p no:cacheprovider --tb=no --junitxml="$evidence/old_frozen_tests.xml" --basetemp="$evidence/old_frozen_tmp"
Remove-Item Env:CORE_AUDIT_SOURCE
wsl -d Ubuntu -- env PYTHONPATH=/mnt/e/AdaSelectPP-core-slimming-v2-results/minimal_env/Lib/site-packages python3 -B -m pytest tests/core/test_supervisor_subprocess.py tests/core/test_execution_audit.py -q -p no:cacheprovider --junitxml=/mnt/e/AdaSelectPP-core-slimming-v2-results/execution_audit_fix/linux_final_verified_tests.xml --basetemp=/tmp/core-execution-audit-linux-verified-20260918
```

The archived baseline was extracted with `git archive` into a new evidence directory.
No existing checkout was reset, cleaned or replaced. Evidence, exact commit and diff are
recorded in the adjacent external `execution_audit_fix/FINAL_REPORT.md` and revision receipt.
No new performance result, nine-workload acceptance or significance claim is made.
