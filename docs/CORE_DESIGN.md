# Core v2 design and behavior changes

Component reference: `97d925989e53bdeea282be3b7ec66ef6c221f251`, measured M2Q mode. The complete controller intentionally differs because diagnostic charges and count truncation are removed, and the first tuning round is budgeted.

## Four modules

| Module | Actual implementation | Rules |
|---|---|---|
| CandidateGenerator | `adaselect_pp/candidate_gen_v2/generator.py` | Probe in internal rounds 0–1, then mature-positive-seed prefix growth; original parser, PK/UNIQUE filter, width-first caps, and bounded width-2 fairness. |
| BenefitEstimator | `AdaSelect._initial_costs`, `_test_candidate`, `_calculate_adaptive_lambda` | New, uninstalled width-2 ADD evaluates every current occurrence against the complete protected root. All width-1 and KEEP observations retain the legacy mask/fallback formula and are explicitly approximate. Signed deltas, Trigg, EWMA, absent-history decay remain unchanged. |
| BudgetScheduler | `AdaSelect.bind_window`, `_ordered_candidates`, `_estimate_benefits`; `QCPBudget`; backend `Meter` | Full pruned appearing set, fixed ordinary history/IndexKey order, at most one original structural nominee promoted in grow. Scan once; fit, measure, or defer and continue. |
| ConservativeSelector | `AdaSelect._choose_config`; release driver `OwnedRoot.transition` | Original log-positive normalization, creation cost, stable Top-K/filter and symmetric beta including sign/zero/first-round branches; physical DDL follows actual state. |

The estimator, scheduler and selector deliberately share one small orchestrator, rather than introducing an alternative online framework. `batch.stage`, candidate metadata and seed/history dictionaries are control state. Metrics are outputs and are never queried to choose candidates or stage.

## Four inputs and transparent constants

Only `max_num`, `qcp_budget`, `alpha`, `beta` are public policy inputs. Missing/unknown keys, bool/fraction/string/negative QCP values, and retired ratio CLI/environment aliases fail explicitly. `RuntimeConfig` contains environment inputs, not candidate allowances. Offline migration requires an explicit QCP amount/vector and rejects enabled unsupported mechanisms.

`POLICY_DEFAULTS` holds effective candidate supply, scoring/confidence, growth, structural rank, creation fallback, Trigg/sign/RSFE, comparison epsilon and identity tolerance constants. Generation still has per-query 12, per-table 4, round-table 6, total `max(1, 4*max_num+6)` bounds, widths 1–2 and width-2 reserve 1/table, 4/round. These are algorithm constants, not removed parameters. Alpha/beta and all 603 registered caps remain original profile values. The Core has no benchmark-specific dispatch cap.

`optimizer_ratio` and its aliases have been removed from Core constructors, JSON config loading, active shell entry and scheduling. Old main/configs/sweep scripts are isolated under `experiments/reproduce`; archived ratios are read only by offline reporting, never by the release driver or policy. There is no `r=1` compatibility branch, count slice, count-exhaustion return, JOB quota or replacement acquisition score.

## Dispatch and accounting invariants

1. Root and every target occurrence pass the same real EXPLAIN guard. Charge occurs before the attempt; failures remain paid. Safety/catalog SQL is timed but is not optimizer QCP. Runtime EXPLAIN ANALYZE belongs to execution, not recommendation QCP.
2. The physical window ID binds one counter. Internal timeout resets clear original history/configuration state but never refresh that counter; repeated processing of a window is rejected. The next physical window starts its own registered cap.
3. A baseline must fit completely. Incomplete/failed roots HOLD without generating candidates, fake zero roots, or history updates.
4. Ordinary order is a snapshot of normalized positive history plus IndexKey. Grow promotes the original eligible/ranked structural nominee even when the obsolete count allowance would be below two. No candidate is added by metadata alone.
5. Every appearing candidate is visited once. The atomic requirement is the full window for uninstalled width-2 ADD, otherwise the occurrence-mask count. Nonfit work is deferred and the scan continues. No reservations, retries, dedup cache, carry, exploration or SWAP exist.
6. Zero-request NO_HIT is processed once even at zero balance; it retains original gated legacy-history behavior and is not labeled a valid FULL observation.
7. A failed/incomplete/identity-invalid full target never updates benefit history. Per-call epoch/catalog checks and final logical target restoration are retained. Identity/safety errors stop live work.
8. After the scan every deferred task must require more than the remaining budget. Unspent QCP is legal after all eligible work has been attempted or when no eligible atomic task fits.

## Explicit changes

| ID | Change | Effect |
|---|---|---|
| D01 | Fix the already accepted generator/full-ADD/adaptive/symmetric modes | Same fixed-state component semantics. |
| D02 | Delete paid prefix replacement diagnostics and shadow calculations | Zero diagnostic requests; those resources may now serve candidate measurements. No measured causal speedup is inferred. |
| D03 | Delete repeated diagnostic JSON/trace/funnel output | Lower reporting work is possible; observer tests prohibit SQL or policy feedback. |
| D04 | Separate stage/seed/metadata from metrics | State is retained; report field names no longer drive control. |
| D05 | Strict four-input config and offline conversion | Unsupported old modes fail explicitly. |
| D06 | Remove research/action/learned modules from the Core import graph | Minimal dependencies; historical code/evidence remains addressable. |
| D07 | Remove ratio and candidate-count cutoff | Entire appearing pool can receive measurement; histories and subsequent choices may diverge from old M2Q. |
| D08 | Apply QCP to the first/internal-reset tuning round | No unlimited first-round evaluation; insufficient baseline HOLD. First-round selector semantics remain original. |
| D09 | Remove active ratio from release profiles | Original trace/order/alpha/beta/timeout/caps are checked against the accepted registration; no budget recomputation. |

D02 and D07 are different resource/eligibility changes, not pure refactoring. Offline reporting may classify a new observation against the old count boundary using the *same observed Core state*. This is not a reconstructed old trajectory, a D02/D07 causal decomposition, or a performance comparison.

## Safety and output

Accepted transport files retain their hashes; adapter safety functions, transaction repair (`autocommit=False` for measurement), owned DDL, failure cleanup and recovery retain AST identity. No UPDATE pg_index fallback or run-level replacement for per-call validation was introduced. A filename containing `action_scope` is transport only; action runtime/selector modules are never imported.

Default policy output is `run_manifest.json`, compact `rounds.csv`, and error/DDL events. The accepted adapter's `initial_catalog.json` and `postflight.json` are retained as DDL ownership/recovery receipts: these two safety receipts are not per-candidate diagnostic dumps. They let the unchanged parent recovery path restore only journaled owned indexes after a worker failure. The offline report is generated after execution. No optimizer response cache or detailed paid-request ledger is loaded online.

Inclusive recommendation = whole run wall minus execution wall and transition wall; setup, metadata, validation, safety and reporting overhead remain included. Execution retains original actual-plan milliseconds and original timeout penalty. Total = execution + inclusive recommendation + transition; it need not equal wall because the execution metric is historical protocol time. EXPLAIN, scope-exclusive and other recommendation components are separate. Actual DDL creation/drop attempts and cleanup are separately recoverable from the unchanged journal.

## Validation boundary

Active tests cover the Q1–Q12 cases, fixed-state generator/selector/history reference comparisons, 178-case individual migration, safety cleanup and minimal imports. `experiments/core_v2/replay.py` drives the actual method with 490 captured observations (237 full width-2 ADD, 253 legacy width-1), including 73 targeted recoveries. It checks exact source/root/epoch/occurrence identities and compares new history/call sequences with the 97d9259 reference. It does not fill missing optimizer responses or prove 603-round closed-loop equivalence.

Only the final clean revision is registered for nine live workloads. Actual counts/results are written outside the source worktree and are not substituted with historical scores. No source is tuned in response to measured performance.
