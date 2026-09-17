# Width-2 ADD measurement repair v1

Policy parent: `1a1129ad3b2dc512a3951aca9cdada97d966e774`.

`width2_add_full_window` defaults to false. Only a natively nominated,
uninstalled width-2 ADD measures the entire current occurrence window when
enabled. Completed observations use the existing adaptive lambda and EWMA.
Incomplete full measurements return an explicit status without changing history.
The safe backend must validate the root/window identity before and after them.
Width-1, KEEP, generation, nomination, ranking, Top-K, beta, creation penalty,
and overlay configuration retain the original policy semantics.

`QCPBudget` is an optional experimental cutoff, not a scheduler. The original
candidate count budget and nomination order remain intact. A nominated task
that cannot fit is deferred; later original nominations are still checked.
No reservations, cache, retries, carry, or extra nominations are introduced.

## Frozen experiment inputs

User package: `adaselectpp_width2_mask_fix_v1.zip`, with
`IMPLEMENTATION_SPEC.md` and `EXPERIMENT_CONTRACT.json`. The external registration
records their hashes and embeds the complete contract unchanged.

Diagnostic source manifest SHA-256:
`580cecc4ce794639a02a0585aae3038898ac8326f6629115be8eb1d388f025b9`.

Accepted adapter `tool/live_support.py` SHA-256:
`78382edcbf93cfa8c110a06253f7f3c604f3da81fd76cfdab08e7f39a023573f`.
It supplies the validated HypoPG transport, transactional measurement connection,
owned-index DDL and cleanup. No action selector, runtime or dispatch is imported.

The runner verifies source, trace, database, schema/statistics, protected indexes,
and per-round QCP accounting. LQ is explicitly a common-QCP cutoff wrapper
control. M2Q changes only the width-2 ADD mask. Each physical input window uses
its registered cap even after a native timeout reset.

## Validation and stop condition

`tools/width2_mask_replay.py` calls the actual patched method with complete
per-query ledger responses and checkpoint history. It compares 490 disabled
observations and states with the original method, 237 repaired width-2 and 253
unchanged width-1 targets, and the native call sequence in all 603 windows.
The supplied output checker independently reconciles the 980 mode rows.

`tools/width2_mask_campaign.py` executes exactly the registered nine paired
workloads (18 runs, 1206 rounds), with a four-hour campaign deadline, 15-minute
cleanup grace, 30 preflight EXPLAIN maximum, and 44542 policy QCP maximum.
The first pair is the live smoke; successful completion continues the registered
order. Failure is retained and stops the campaign. No automatic restart,
additional repeat, exploration, SWAP or production merge is authorized.

Primary total is original-protocol execution plus inclusive recommendation plus
transition including cleanup. The report separates EXPLAIN, non-EXPLAIN HypoPG
scope, and other recommendation overhead. Results are screening only, without
statistical significance or payback claims.
