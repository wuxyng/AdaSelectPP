# PR21d Validation Artifact Inventory

PR21d is documentation and artifact inventory only. It records what evidence
already exists in the PR20c, PR20d, PR20e, and PR20f artifacts, what fields are
usable for future offline/shadow validation, and what evidence remains missing.

It does not implement validation, PR21b-online behavior, selector changes,
`_choose_config()` changes, candidate generation changes, scoring changes,
budget changes, `optimizer_ratio` changes, materialization changes, cooldown,
payback, overlay, beta, or DML runtime behavior.

## Current State

Merged prerequisites:

```text
PR #28: PR21b design/spec
PR #29: Phase 0.5 prefix-swap findings
PR #30: PR21c offline/shadow validation roadmap
```

PR21b-online remains blocked.

PR21d is the next planning layer after PR21c. It does not decide whether a
prefix-upgrade lane is safe enough. It only inventories the existing evidence
available for future offline/shadow validation.

## Scope

The inventoried artifacts are:

```text
runs_pr20c_swap_width2_oracle/
runs_pr20d_real_exec_prefix_swap/
runs_pr20e_broader_prefix_swap_replay/
runs_pr20f_negative_control_prefix_swap_replay/
```

The dominant observed pattern remains:

```text
movie_info(mi_movie_id)
->
movie_info(mi_movie_id, mi_info_type_id)
```

The PR21b operator boundary remains:

```text
C - {(T,c1)} + {(T,c1,c2)}
```

This inventory must not broaden that into arbitrary replacement, width-3
expansion, predictive workload synthesis, online exploration, or joint
candidate-generation/selector optimization.

## Inventory Summary

| Source | Primary artifact files | Existing reusable evidence | Missing or incomplete evidence | Can support | Cannot support |
| --- | --- | --- | --- | --- | --- |
| PR20c | `pr20c_width2_oracle_candidates.csv`, `pr20c_width2_oracle_rounds.csv`, `pr20c_width2_oracle_summary.csv` | What-if add-vs-swap oracle rows, feasible prefix-replacement configs, best swap index by round, dominant target frequency | Real-execution outcome, query-level concentration, storage/write/transition cost, shadow stability | Candidate/action recall checks; prefix-swap opportunity inventory | Online activation; real-exec safety; Gate B calibration |
| PR20d | `pr20d_real_exec_queries.csv`, `pr20d_real_exec_rounds.csv`, `pr20d_real_exec_summary.csv` | Real-exec replay for a biased high-opportunity subset, query-level deltas, plan-use flags, top-query delta share | Broader distribution, rejection arms, storage/write/transition cost, cross-window shadow evidence | Existence evidence for positive prefix-swap arms; concentration diagnostics seed | Distributional proof; rejection safety; online state-space safety |
| PR20e | `pr20e_broader_replay_queries.csv`, `pr20e_broader_replay_rounds.csv`, `pr20e_broader_replay_summary.csv`, `pr20e_broader_replay_excluded_unstable.csv` | Broader positive-arm replay, top/mid/low strata, CV fields, run order, plan-use counts, query-level concentration, descriptive ordering diagnostic | Negative controls, false-accept safety, storage/write/transition cost, full cross-workload evidence | Positive-arm recall validation; near-margin/sign-stability review for positive arms | Online activation; global ROC; what-if calibration claim |
| PR20f | `pr20f_negative_control_queries.csv`, `pr20f_negative_control_rounds.csv`, `pr20f_negative_control_gate_metrics.csv`, `pr20f_negative_control_summary.csv`, `pr20f_negative_control_excluded_unstable.csv` | Rejection-arm stress subset, threshold-level false accept/reject metrics, non-positive what-if cases, near-margin cases, plan-use counts, query-level concentration, CV fields | Global ROC, complete storage fields, write-maintenance cost, transition cost, shadow-observed evidence | Gate A failure evidence; false-accept safety test design; online-reject replay reporting | Proof of Gate B safety; online activation |

## PR20c Width-2 Oracle Artifacts

Files:

```text
runs_pr20c_swap_width2_oracle/pr20c_width2_oracle_candidates.csv
runs_pr20c_swap_width2_oracle/pr20c_width2_oracle_rounds.csv
runs_pr20c_swap_width2_oracle/pr20c_width2_oracle_summary.csv
```

Observed inventory:

- candidate rows: 189;
- round rows: 25;
- summary rows: 1;
- swap-win rounds: 17;
- add-win rounds: 0;
- mean best add relative improvement: 0;
- mean best swap relative improvement: 0.034018514721;
- max best swap relative improvement: 0.169581355201.

The summary conclusion is:

```text
width-2 value is primarily replacement value; selector-level retain/swap is needed.
```

Reusable fields:

- `baseline_config`;
- `add_config`;
- `swap_prefix_index`;
- `swap_config`;
- `add_relative_improvement`;
- `swap_relative_improvement`;
- `best_mode`;
- `oracle_pass_add`;
- `oracle_pass_swap`;
- `best_swap_index`.

PR20c can support future offline recall tests by checking whether validation
pipelines still rediscover the same bounded prefix-upgrade opportunities, with
the same narrow action shape.

PR20c cannot support online activation because it has no real-execution
outcomes, no query-level concentration, no storage/write-maintenance evidence,
and no shadow state evidence.

## PR20d Real-Execution Prefix-Swap Artifacts

Files:

```text
runs_pr20d_real_exec_prefix_swap/pr20d_real_exec_queries.csv
runs_pr20d_real_exec_prefix_swap/pr20d_real_exec_rounds.csv
runs_pr20d_real_exec_prefix_swap/pr20d_real_exec_summary.csv
```

Observed inventory:

- query rows: 198;
- round rows: 6;
- winning rounds tested: 5;
- control rounds tested: 1;
- improved rounds at threshold: 6;
- flat or worse rounds: 0;
- mean execution relative improvement: 0.189035773213;
- median execution relative improvement: 0.110522468877;
- max execution relative improvement: 0.433088626734;
- prefix plan-used query count: 108;
- composite plan-used query count: 108;
- mean top-query delta share: 0.782174397561.

Reusable fields:

- `round_role`;
- `baseline_config`;
- `swap_config`;
- `prefix_index`;
- `composite_index`;
- `pr20c_swap_relative_improvement`;
- `baseline_exec_ms_median`;
- `swap_exec_ms_median`;
- `exec_relative_improvement`;
- `prefix_plan_used_query_count`;
- `composite_plan_used_query_count`;
- `positive_query_count`;
- `top_query_delta_share`;
- query-level `plan_uses_prefix_index`;
- query-level `plan_uses_composite_index`.

PR20d can support existence evidence for the dominant positive arm and provide
seed diagnostics for query-level concentration and plan-use checks.

PR20d cannot support distributional claims because it is a small,
high-opportunity-biased replay. It also does not provide rejection-arm safety,
storage/write-maintenance cost, transition-cost evidence, or future online
state-space evidence.

## PR20e Broader Positive-Arm Replay Artifacts

Files:

```text
runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_queries.csv
runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_rounds.csv
runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_summary.csv
runs_pr20e_broader_prefix_swap_replay/pr20e_broader_replay_excluded_unstable.csv
```

Observed inventory:

- query rows: 429;
- round rows: 13;
- excluded unstable rows: 0;
- `top_win` rounds: 5;
- `mid_win` rounds: 4;
- `low_win` rounds: 4;
- improved rounds: 12;
- flat rounds: 1;
- worse rounds: 0.

Summary by stratum:

| Stratum | Rounds | Improved | Flat | Worse | Mean real-exec relative improvement | Median real-exec relative improvement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `top_win` | 5 | 5 | 0 | 0 | 0.148676837924 | 0.0882713260487 |
| `mid_win` | 4 | 4 | 0 | 0 | 0.0322628614343 | 0.0347689133964 |
| `low_win` | 4 | 3 | 1 | 0 | 0.0274779044833 | 0.0277532003093 |

The ordering diagnostic is descriptive only:

```text
DESCRIPTIVE ONLY: ordering agreement, not calibration.
```

Reusable fields:

- `sample_category`;
- `pr20c_whatif_rel_improvement`;
- `baseline_config`;
- `swap_config`;
- `prefix_index`;
- `composite_index`;
- `baseline_exec_ms_all`;
- `swap_exec_ms_all`;
- `baseline_cv`;
- `swap_cv`;
- `run_order_id`;
- `run_order`;
- `real_exec_rel_improvement`;
- `outcome`;
- `plan_uses_prefix_count`;
- `plan_uses_composite_count`;
- `query_level_concentration`;
- query-level plan-use flags.

PR20e can support positive-arm recall validation: future offline/shadow
validation should keep these known positive cases visible and should report
when a proposed Gate B design fails to recall them.

PR20e cannot support rejection safety because it is positive-arm focused. It
also cannot support online activation, global ROC claims, or what-if
calibration claims.

## PR20f Negative-Control Replay Artifacts

Files:

```text
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_queries.csv
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_rounds.csv
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_gate_metrics.csv
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_summary.csv
runs_pr20f_negative_control_prefix_swap_replay/pr20f_negative_control_excluded_unstable.csv
```

Observed inventory:

- query rows: 561;
- gate-evaluation rows: 68;
- excluded unstable rows: 0;
- tested thresholds: 0.01, 0.02, 0.03, 0.05;
- stress subset rounds per threshold: 17;
- threshold-expanded outcomes: 32 improved, 24 flat, 12 worse.

Gate A metrics:

| Threshold | Tested | Accept | Reject | False accept rate | False reject rate |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.01 | 17 | 12 | 5 | 0.416666666667 | 0.2 |
| 0.02 | 17 | 12 | 5 | 0.416666666667 | 0.2 |
| 0.03 | 17 | 6 | 11 | 0.333333333333 | 0.363636363636 |
| 0.05 | 17 | 2 | 15 | 0 | 0.4 |

Summary by category:

| Category | Rounds | Improved | Flat | Worse | Mean real-exec relative improvement | Median real-exec relative improvement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `non_target_best_positive` | 5 | 3 | 1 | 1 | 0.0288763703184 | 0.0524370417816 |
| `predicted_negative` | 3 | 0 | 3 | 0 | -0.00376049268907 | -0.00410885068963 |
| `near_margin` | 9 | 5 | 2 | 2 | 0.0398946168973 | 0.0161387017462 |

Reusable fields:

- `sample_category`;
- `old_config`;
- `swap_config`;
- `prefix_index`;
- `composite_index`;
- `target_swap_whatif_rel_improvement`;
- `best_swap_index`;
- `best_swap_whatif_rel_improvement`;
- `is_target_best`;
- `gate_threshold`;
- `gate_accept`;
- `gate_reject`;
- `real_exec_rel_improvement`;
- `real_outcome`;
- `gate_outcome`;
- `baseline_cv`;
- `swap_cv`;
- `run_order_id`;
- `run_order`;
- `plan_uses_prefix_count`;
- `plan_uses_composite_count`;
- `query_level_concentration`;
- query-level plan-use flags.

For PR21d inventory purposes, `whatif_gain` is a design-level validation
concept from PR21b/PR21c. In the PR20f artifacts, the closest existing field is
`target_swap_whatif_rel_improvement` for the dominant target swap. Future
validation reports must explicitly state which artifact field instantiates the
design-level `whatif_gain` concept.

PR20f can support rejection-arm validation design by preserving the known Gate A
failure cases. It also provides the current replay source for:

- false-accept reporting;
- false-reject reporting;
- non-positive what-if online-reject reporting;
- near-margin and sign-instability review;
- single-query dominated outcome review.

PR20f cannot prove Gate B safety because it is a stress subset, not a global
ROC. It also has no usable storage/write-maintenance evidence and no
shadow-observed online state evidence.

## Storage, Write, and Transition-Cost Gaps

The PR20f round file contains storage proxy columns:

```text
prefix_index_size_bytes
composite_index_size_bytes
storage_delta_bytes
storage_delta_ratio
```

All four columns are currently empty in the inventoried artifact rows.

Therefore storage delta must remain TODO for PR21b-online purposes. These fields
may define the expected shape of future evidence, but they are not current
evidence.

The inventoried artifacts also do not provide:

- write-maintenance delta;
- build/drop transition cost;
- lock or visibility transition cost;
- per-window materialization churn cost;
- cross-window retention cost for keeping the incumbent prefix.

Until those fields are populated by future offline/shadow evidence, any
candidate that depends on storage, write-maintenance, or transition economics
must remain shadow/deferred.

## AdmissionEvidence and RetentionEvidence Signals

The existing artifacts provide seed signals, not complete online evidence.

AdmissionEvidence for a candidate composite can reuse:

- PR20c `swap_relative_improvement` and feasible `swap_config`;
- PR20d/PR20e/PR20f real-execution deltas;
- PR20d/PR20e/PR20f composite plan-use counts;
- PR20e/PR20f variance and run-order fields;
- PR20f false-accept and false-reject classifications.

RetentionEvidence for the incumbent prefix can reuse:

- query-level `plan_uses_prefix_index`;
- round-level `plan_uses_prefix_count`;
- query-level execution deltas where prefix plans remain useful;
- concentration fields that show whether benefit is broad or single-query
  dominated.

Missing for both evidence types:

- shadow-window observation across multiple future windows;
- `appearing_curr` visibility state;
- `keep_visible` behavior;
- `U_anchor` or equivalent anchor accounting;
- retain/swap visibility state;
- storage/write-maintenance/transition costs;
- evidence that a composite can be admitted without suppressing useful
  incumbent-prefix observations.

The current artifacts can shape the schema for future validation, but they do
not solve the online state-space problem.

## Offline/Shadow Validation Supported by Current Artifacts

Current artifacts are sufficient to define and test offline/shadow reporting
for:

- positive-arm recall against PR20e known positives;
- PR20f rejection-arm false-accept and false-reject reporting;
- non-positive what-if online-activation veto reporting;
- near-margin and sign-instability labels;
- query-level concentration diagnostics;
- plan-use diagnostics for prefix and composite indexes;
- variance/CV exclusion reporting;
- descriptive ordering diagnostics, without calibration claims.

These are validation-reporting capabilities. They do not imply online
activation.

## Evidence Not Sufficient for Online Activation

The current artifact set is not sufficient for PR21b-online because it lacks:

- a validated Gate B state machine;
- broader and less biased rejection-arm coverage beyond the current PR20f
  stress subset;
- cross-window shadow stability;
- complete AdmissionEvidence and RetentionEvidence traces;
- populated storage/write-maintenance/transition-cost evidence;
- online visibility semantics for `appearing_curr`, `keep_visible`, `U_anchor`,
  retain, and swap states;
- anti-churn evidence across workload windows;
- evidence beyond JOB random and the single dominant `movie_info` pattern.

The correct conclusion is not that prefix upgrade is low value. The correct
conclusion is that the existing evidence justifies offline/shadow validation
work, while online implementation remains blocked.

## PR21d Acceptance Criteria

PR21d is complete when the documentation records:

1. which PR20c/PR20d/PR20e/PR20f artifacts exist;
2. which fields can be reused for future offline/shadow validation;
3. which fields are missing or empty;
4. which artifacts support positive-arm recall validation;
5. which artifacts support rejection-arm safety validation;
6. how `whatif_gain <= 0` can be counted as an online-activation veto in
   replay;
7. how near-margin, sign-instability, and single-query dominated cases can be
   flagged;
8. which storage/write-maintenance/transition-cost items remain TODO;
9. why the current evidence can support validation design but cannot support
   PR21b-online activation.

No code, tests, runtime behavior, or materialization behavior should change in
PR21d.
