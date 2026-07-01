# PR21c Offline/Shadow Validation Roadmap

PR21c is documentation only. It turns the PR21b prefix-upgrade guardrails into
offline and shadow validation requirements. It does not implement online
behavior.

This phase must not change runtime code, `_choose_config()`, selector logic,
online policy, candidate generation, scoring or benefit logic, the evaluation
budget, `optimizer_ratio`, materialization policy, cooldown, payback, overlay,
beta, or DML runtime behavior.

## Current State

PR21b-design/spec is closed by:

```text
f3a7b83 docs: tighten PR21b prefix-upgrade design guardrails
```

PR21b-online remains blocked.

PR21c is the next planning layer. It defines what offline and shadow evidence a
future implementation proposal would need before any PR21b-online work can be
reopened.

## Validation Boundary

The narrow operator remains:

```text
C - {(T,c1)} + {(T,c1,c2)}
```

The motivating observed pattern remains:

```text
movie_info(mi_movie_id)
->
movie_info(mi_movie_id, mi_info_type_id)
```

PR21c must not generalize this into arbitrary replacement. The validation
roadmap must preserve:

- no arbitrary drop-any/add-any swap;
- no non-prefix replacement;
- no hidden candidate synthesis;
- no width-3 expansion;
- no online exploration;
- no materialization-policy change by implication.

## Evidence Questions

Future offline/shadow validation workstreams defined by PR21c should answer
these questions before any online proposal:

1. Does the proposed action preserve the PR21b prefix-upgrade operator boundary?
2. Is the composite prefix-position-aware, with exact leading-key preservation?
3. Does the workload-level what-if signal pass the one-sided eligibility veto?
4. Are near-zero or sign-unstable cases correctly kept in shadow/deferred
   status?
5. Do positive-arm cases remain positive under real-execution replay or
   shadow-observed evidence?
6. Do rejection-arm and negative-control cases show improved false-accept
   safety relative to PR20f Gate A behavior, with false accepts and false
   rejects reported separately?
7. Is benefit broad enough, or is the round dominated by one transient query?
8. Does the incumbent prefix still show retention evidence for part of the
   workload?
9. Does net benefit remain plausible after storage, write-maintenance, and
   transition-cost evidence is considered?
10. Is the evidence stable across workload windows without prefix/composite
    flip-flop?

These are validation questions, not implementation hooks.

## Gate B Safety Validation Checklist

PR21c's central question is:

```text
How do we validate whether the PR21b Gate B is safe enough?
```

The answer must be an offline/shadow checklist, not an online swap
implementation.

### 1. PR20e Positive Arms As Recall Tests

PR20e positive-arm rounds should be reused as recall tests for the future Gate B
design. A candidate Gate B that rejects or indefinitely defers most previously
validated positive-arm cases needs explanation before it can be considered
useful.

This is a recall test, not an activation rule. Passing the PR20e recall check
does not authorize online swaps.

### 2. PR20f Rejection Arms As False-Accept Safety Tests

PR20f rejection-arm and negative-control rounds should be reused as
false-accept safety tests. A candidate Gate B must show that it does not repeat
Gate A's failure mode: accepting too many flat or worse swaps under scalar
what-if thresholding.

The primary safety question is whether flat/worse rejection-arm cases remain
`REJECT` or `SHADOW_DEFER`, not whether the gate maximizes accepted positives.

### 3. Counting The Non-Positive What-if Veto

Replay and shadow reports should count proposals where:

```text
whatif_gain <= 0 => ONLINE_REJECT
```

Those proposals should be reported separately from operator-ineligible cases.
If real-execution replay or shadow-observed evidence conflicts with the
non-positive what-if signal, the conflict should be retained for analysis while
the proposal remains ineligible for online activation.

The report should distinguish a primary online-activation eligibility status
from orthogonal diagnostic flags.

Primary status may include:

- `operator_ineligible`;
- `online_reject_nonpositive_whatif`;
- `shadow_defer_positive_whatif`;
- `future_high_confidence_eligible`.

Orthogonal diagnostic flags may include:

- `near_margin`;
- `sign_unstable`;
- `conflicting_real_or_shadow_evidence`;
- `single_query_dominated`;
- `missing_storage_or_maintenance_evidence`.

A near-zero non-positive proposal remains online-ineligible under the
non-positive what-if veto, but it may still carry `near_margin` or
`sign_unstable` flags for offline/shadow analysis. These labels and flags are
validation/reporting concepts, not runtime states.

### 4. Near-Margin And Sign-Instability Marking

Near-margin and sign-unstable cases must be marked explicitly. A proposal should
be treated as near-margin or sign-unstable when replay windows, repeats, or
artifact versions disagree on the direction or practical meaning of the
workload-level signal.

PR21c does not set a numeric margin. It requires that near-margin and
sign-unstable cases remain `SHADOW_DEFER` unless a later validated
conflict-resolution rule exists.

### 5. Single-Query-Dominated Win Detection

Replay and shadow reports must detect whether a workload-level win is dominated
by one query. The report should compare round-level improvement with per-query
contribution patterns and mark cases where a single query accounts for most of
the apparent gain while other queries are flat or worse.

This detection is descriptive. PR21c does not define a dominance threshold, but
it requires the concentration pattern to be visible before any future online
discussion.

### 6. Existing Signals For Admission And Retention Evidence

PR21c should use existing trace, replay, and artifact signals where available.
It must not require new runtime trace fields in this phase.

`AdmissionEvidence(u)` for composite `u = (T,c1,c2)` should be derived from
available signals such as:

- exact prefix-position eligibility for `(T,c1,c2)`;
- composite presence in existing candidate or replay artifacts;
- composite evaluation status in what-if artifacts;
- positive workload-level what-if signal;
- real-execution replay or shadow-observed improvement;
- composite plan-use evidence when replay plans are available;
- repeated appearance across workload windows.

`RetentionEvidence(p)` for incumbent prefix `p = (T,c1)` should be derived from
available signals such as:

- prefix presence in the old or baseline configuration;
- prefix selected/incumbent status;
- prefix plan-use evidence in baseline replay when available;
- per-query regressions after replacement;
- workload slices where prefix retention remains beneficial;
- evidence that the composite win is single-query dominated.

Missing admission or retention evidence should be reported as missing, not
filled in by assumption.

### 7. Missing Storage Or Write-Maintenance Delta Forces Shadow/Defer

If storage delta, write-maintenance delta, or transition-cost evidence is
missing, the proposal cannot become future high-confidence eligible. It must
remain `SHADOW_DEFER` even if query-runtime evidence is positive.

PR20f storage proxy fields were unavailable/TODO. They must stay marked as
unavailable until the size and maintenance evidence path is fixed and validated.

### 8. Criteria Before Discussing PR21b-Online

PR21b-online can only be discussed after offline/shadow validation has evidence
for all of the following:

- PR20e positive-arm recall is preserved or explained;
- PR20f rejection-arm false-accept safety improves over Gate A behavior;
- non-positive what-if proposals are counted and kept online-ineligible;
- near-margin and sign-unstable proposals are marked and deferred;
- single-query-dominated wins are visible;
- `AdmissionEvidence(u)` and `RetentionEvidence(p)` are both represented from
  available evidence;
- missing storage, write-maintenance, or transition-cost evidence forces
  shadow/defer;
- results are reported within benchmark and pattern boundaries.

Meeting these criteria only permits discussion of a later online design. It
does not implement PR21b-online.

## Required Validation Workstreams

### V0. Scope And Artifact Inventory

Identify the offline artifacts and shadow-observation sources that will be used
for validation. At minimum, the inventory should distinguish:

- PR20c what-if swap evidence;
- PR20d/PR20e positive-arm real-execution replay;
- PR20f rejection-arm and negative-control replay;
- any future shadow-observed evidence;
- unavailable or TODO storage and maintenance evidence.

The inventory must mark which evidence is current and which evidence would
require future collection. It must not fabricate storage or maintenance
conclusions from PR20f TODO fields.

### V1. Operator Eligibility Audit

Validate that each proposal is exactly a prefix-to-composite replacement:

```text
C - {(T,c1)} + {(T,c1,c2)}
```

The audit must reject or exclude:

- composites that do not preserve `(T,c1)` as the leading key;
- `(T,c2,c1)` when the incumbent prefix is `(T,c1)`;
- same-column-set overlap without leading-key preservation;
- payload-only column overlap;
- proposals where the incumbent prefix is not selected or otherwise not an
  incumbent;
- proposals where the composite was not already available through existing
  candidate visibility.

This workstream validates action-space scope only. It must not synthesize
candidates.

### V2. What-if Eligibility Veto

Apply the PR21b one-sided veto as a validation rule:

```text
whatif_gain <= 0 => ONLINE_REJECT
```

This is only an online-activation eligibility veto. It is not proof that the
upgrade is harmful, and it must not suppress offline or shadow evidence. If
real-execution replay or shadow-observed evidence conflicts with non-positive
what-if evidence, the conflict remains part of the validation record while the
proposal remains ineligible for online activation.

Positive what-if evidence is not an accept rule. It only allows the proposal to
continue into offline or shadow evidence review.

### V3. Positive-Arm Replay Validation

Re-check that positive-arm cases remain positive under real-execution replay or
shadow-observed evidence. This workstream should preserve the PR20d/PR20e
interpretation boundary:

- evidence is strongest for the observed `movie_info` target pattern;
- ordering diagnostics are descriptive only, not calibration;
- the validation must not claim what-if systematically underestimates or
  overestimates real execution;
- single-workload evidence must not be generalized to all workloads.

### V4. Rejection-Arm And Negative-Control Validation

Validate rejection behavior explicitly. The roadmap must require negative
controls because PR20f showed that scalar Gate A was not safe enough.

This workstream should include:

- non-target-best cases;
- predicted-low cases;
- predicted-negative or non-positive what-if cases;
- near-margin or sign-unstable cases;
- flat/worse real-execution outcomes.

The output should separate false-accept risk from false-reject risk. It should
not choose numeric thresholds in PR21c.

### V5. Query-Level Concentration Review

For every replayed or shadow-observed proposal, validation must make query-level
concentration visible. A round that improves because one query improves
dramatically while many queries regress modestly is not equivalent to a broad
workload improvement.

The review should distinguish:

- repeated modest regressions;
- dramatic plan-change wins;
- single-query-dominated rounds;
- broadly distributed gains;
- unstable per-query labels across replay runs.

This requirement is descriptive. PR21c does not define a concentration
threshold.

### V6. Incumbent Retention Evidence

Validation must represent admission evidence for the composite separately from
retention evidence for the incumbent prefix:

- `AdmissionEvidence(u)`: evidence that the composite deserves entry;
- `RetentionEvidence(p)`: evidence that the incumbent prefix still deserves
  retention.

A visible composite is not evidence that the incumbent prefix is safely
droppable. If the prefix remains useful for part of the workload, validation
must preserve that evidence.

This roadmap does not implement incumbent re-evaluation, `U_keep`, `U_anchor`,
or `_choose_config()` changes.

### V7. Net-Benefit Completeness Review

Validation must treat query runtime as incomplete by itself. Before any future
online proposal, the evidence record should state whether these terms are
available, unavailable, or TODO:

- query-runtime delta;
- storage delta for prefix versus composite;
- write-maintenance delta;
- transition cost;
- build-before-drop transient cost, if relevant.

PR21c does not define a binding NetBenefit formula. It only requires that
missing non-runtime costs remain explicit blockers or caveats.

### V8. Shadow Stability And Anti-Churn Review

Shadow validation should check whether a proposal remains stable across
workload windows. The review should look for:

- repeated evidence for the same prefix-upgrade proposal;
- sign instability near zero;
- prefix/composite flip-flop risk;
- no-immediate-reversal evidence;
- delayed-actuation risk under workload drift.

This is an offline/shadow review requirement. It does not implement cooldown,
payback, overlay, beta, or materialization behavior.

### V9. Release-Gate Readiness Review

PR21b-online can only be reconsidered after PR21c validation shows that the
future Gate B design has evidence for:

- action-space correctness;
- positive-arm behavior;
- rejection-arm behavior;
- near-margin deferral;
- query-level concentration;
- incumbent retention;
- net-benefit completeness;
- anti-churn stability;
- benchmark and pattern boundaries.

Passing this review would justify a later design or implementation proposal. It
would not itself implement online behavior.

## Required Roadmap Outputs

Future PR21c follow-up work should produce documents or reports that state:

- which proposals were in scope;
- which proposals were rejected by operator eligibility;
- which proposals were rejected by non-positive what-if eligibility veto;
- which proposals stayed shadow/deferred due to near-margin or conflicting
  evidence;
- which proposals had positive-arm replay support;
- which proposals failed negative-control review;
- whether benefit was query-concentrated or broadly distributed;
- whether incumbent retention evidence was present;
- whether storage, maintenance, and transition-cost evidence was available;
- whether the evidence remains limited to JOB random and the dominant
  `movie_info` pattern.

These are reporting requirements. They are not code-level trace-field
requirements for this phase.

## Non-Goals

PR21c does not:

- implement PR21b-online;
- modify `_choose_config()`;
- change selector logic;
- change online policy;
- change candidate generation;
- change scoring or benefit logic;
- change the evaluation budget;
- change `optimizer_ratio`;
- change materialization policy;
- change cooldown, payback, overlay, beta, or DML runtime behavior;
- add runtime trace fields;
- add tests that alter behavior;
- define numeric thresholds;
- define a binding NetBenefit formula;
- define an implementation state machine;
- introduce MAB/RL implementation;
- introduce online exploration.

## Acceptance Criteria

PR21c-roadmap is complete when it:

- is documentation only;
- preserves PR21b-online as blocked;
- converts PR21b guardrails into offline/shadow validation workstreams;
- keeps the prefix-upgrade operator narrow;
- requires both positive-arm and rejection-arm validation;
- preserves the non-positive what-if online-activation veto;
- treats positive what-if as shadow/deferred evidence, not acceptance;
- requires query-level concentration review;
- requires separate incumbent retention evidence;
- requires storage, write-maintenance, and transition-cost status;
- avoids formulas, thresholds, state machines, or runtime trace schemas;
- makes clear that any future online work belongs to a later phase.

## Current Conclusion

PR21c-offline/shadow-validation-roadmap is a docs-only planning phase.

PR21b-online remains blocked.

The next valid work is offline/shadow validation planning and artifact review,
not online implementation.
