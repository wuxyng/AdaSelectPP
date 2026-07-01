# PR21b Prefix-Swap Design Spec

PR21b-design is documentation only. It defines requirements and guardrails for a
future subsumption-aware prefix-swap / prefix-upgrade lane. It does not
implement online behavior.

This document must not be read as approval to change AdaSelectPP runtime code.
In particular, this phase does not change `_choose_config()`, selector logic,
candidate generation, scoring or benefit logic, the evaluation-budget formula,
`optimizer_ratio`, materialization policy, cooldown, payback, overlay, or beta
behavior, or DML runtime behavior.

## Milestone Context

Phase 0.5 is closed at merge commit:

```text
1a1129ad3b2dc512a3951aca9cdada97d966e774
```

The official candidate-generation mode after Phase 0.5 is:

```text
candidate_generation_mode = probe_grow_fair
```

Current phase:

```text
PR21b-design/spec only
```

Blocked:

```text
PR21b-online
```

## Evidence Basis

PR19 validated candidate-pool quality. It showed that `probe_grow_fair`
improves or reallocates bounded candidate-pool quality without changing online
policy.

PR20a showed that JOB has denser raw and appearing width-2 supply than
TPCH/TPCHS, while width-2 evaluation coverage remains low under the current
bounded evaluation path.

PR20b showed that increasing JOB `optimizer_ratio` from `0.25` to `0.5` is not
sufficient. The run produced more evaluation budget and more what-if calls, but
selected width-2 remained zero and observed workload cost did not improve.

PR20c found offline what-if prefix-swap opportunity, especially replacement
value where a selected width-1 prefix index is replaced by a width-2 composite
index.

The dominant observed target pattern is:

```text
movie_info(mi_movie_id)
->
movie_info(mi_movie_id,mi_info_type_id)
```

PR20d and PR20e confirmed real-execution positive-arm evidence for this
dominant pattern. This is evidence for a real prefix-swap opportunity, not proof
of a complete online selector rule.

PR20f tested rejection-arm / negative-control behavior for the same dominant
pattern. It showed that a simple scalar offline gate is not safe enough for
online activation.

External online tuning literature supports the general importance of merge /
prefix-upgrade-like operations and transition-aware evidence, but it does not
provide additional validation for the specific JOB `movie_info` swap. That
specific evidence remains limited to PR20d, PR20e, and PR20f.

## Gate A Versus Gate B

PR20f tested Gate A, an offline scalar threshold gate:

```text
gate_accept = target_swap_whatif_rel_improvement >= threshold
```

Gate A is not a future online/stateful Gate B.

The PR20f stress subset showed no acceptable scalar threshold:

| threshold | false_accept_rate | false_reject_rate |
|---:|---:|---:|
| `0.01` | `0.417` | `0.20` |
| `0.02` | `0.417` | `0.20` |
| `0.03` | `0.333` | `0.364` |
| `0.05` | `0.00` | `0.40` |

Interpretation:

- low thresholds accepted too many flat or worse swaps;
- high thresholds rejected too many real-execution wins;
- a single scalar what-if threshold is not safe enough;
- PR21b-online remains blocked.

Future Gate B must be stateful and evidence-aware. It cannot be a direct online
copy of Gate A.

## Design Problem

Candidate generation is no longer the immediate bottleneck for the observed JOB
prefix-swap question. The bottleneck has shifted to configuration-level
selection: deciding whether an existing selected width-1 prefix should be
retained, shadowed, or atomically replaced by a width-2 composite candidate.

The observed mechanism is dual-mode:

- some queries regress modestly and repeatedly after the prefix is replaced;
- some queries improve dramatically because the composite enables a different
  plan;
- round-level improvement can be dominated by one query.

Therefore a future design must reason about workload-level net benefit and
query-level concentration. It must not blindly upgrade every selected prefix to
an available composite.

## Terms

Prefix index:

```text
(T, c1)
```

Composite index:

```text
(T, c1, c2)
```

Prefix-subsuming candidate:

```text
(T, c1, c2) can serve leading-column access paths for (T, c1)
```

Prefix swap:

```text
old_config - (T, c1) + (T, c1, c2)
```

Motivating prefix-upgrade operator:

```text
C - {(T,c1)} + {(T,c1,c2)}
```

The motivating example remains:

```text
movie_info(mi_movie_id)
->
movie_info(mi_movie_id, mi_info_type_id)
```

Do not generalize this into arbitrary replacement.

Blind prefix upgrade:

```text
Replace a selected prefix whenever a prefix-subsuming composite exists.
```

Blind prefix upgrade is explicitly forbidden as a design target.

Shadow/deferred decision:

```text
Record and observe a plausible swap without materializing or selecting it.
```

Near-margin cases should remain shadow/deferred unless stronger evidence exists.

## Required Guardrails

### R1. Documentation-Only Scope

PR21b-design must not change runtime behavior. Any future implementation must
be a separate phase or PR after this spec is reviewed.

### R2. Preserve Phase 0.5 Candidate Generation

The future lane must treat `probe_grow_fair` as the official Phase 0.5
candidate-generation mode. It must not reopen candidate generation, widen the
candidate universe, add width-3 candidates, or use selector feedback to mutate
candidate supply.

Candidate generation remains bounded-pool reallocation, not budget expansion.

### R3. Configuration-Level Atomicity

A prefix swap is a configuration-level action. It must be modeled as an atomic
replacement:

```text
remove prefix, add composite
```

The design must avoid intermediate states where the prefix is dropped but the
composite is not retained, visible, or materialized as intended.

Atomicity here refers to the logical configuration-transition decision. A future
physical build-before-drop sequence may temporarily keep both indexes to avoid
dropping the prefix before the composite is ready; this transient physical state
does not expand the PR21b operator action space.

### R4. No Single Scalar Gate

The future online gate must not accept swaps based only on:

```text
target_swap_whatif_rel_improvement >= threshold
```

What-if evidence may be one signal, but not the sole accept/reject rule.

### R5. Evidence Portfolio

A future Gate B should discuss an evidence portfolio that can include:

- workload-level net benefit;
- query-level concentration of benefit and harm;
- repeated modest regressions;
- dramatic plan-change wins;
- stability across replay windows;
- negative-control behavior;
- near-margin uncertainty;
- storage and maintenance deltas once available.

The design must not claim that what-if systematically underestimates or
overestimates real execution. Ordering diagnostics are descriptive only, not
calibration.

### R6. Query-Level Concentration

Because a round can be single-query dominated, future diagnostics must report
whether benefit is concentrated in a small number of queries. A swap that wins
because one query improves dramatically while many queries regress modestly
needs different handling from a swap with broad small wins.

The spec does not prescribe a threshold. It requires the issue to be visible in
the design and in future artifacts.

### R7. Workload-Level Net Benefit

Runtime improvement alone is incomplete. Net benefit must eventually include
storage and maintenance delta for replacing a prefix with a wider composite.

PR20f storage proxy fields were unavailable/TODO in the current artifacts. They
must not be used as evidence until the size-query path is fixed and validated.

### R8. Visibility And State-Space Semantics

Future online design must explicitly define how prefix-swap decisions interact
with online state, including at least:

- `appearing_curr`;
- `keep_visible`;
- `U_anchor`;
- retain/swap visibility;
- whether the prefix remains visible when the composite is shadowed;
- whether the composite remains visible when the prefix is retained;
- how deferred swaps are represented without changing selection behavior.

PR20f validates offline replay/gate behavior only. It does not solve these
online state-space issues.

### R9. No Materialization Policy Change By Implication

A future prefix-swap lane must not smuggle in materialization policy changes.
Selection, retention, visibility, physical creation, and physical drop behavior
must be specified separately.

### R10. Negative-Control First

Future acceptance criteria must include rejection-arm evidence, not only
positive-arm replay. Positive evidence from PR20d/PR20e justifies design work;
it does not justify online activation by itself.

### R11. Near-Margin Handling

Near-margin improved/flat/worse labels are fragile across replay runs. Future
designs should treat near-margin cases as uncertain and default them to
shadow/deferred behavior unless stronger evidence exists.

### R12. Benchmark And Pattern Boundaries

Current evidence is limited to:

- one benchmark/workload: JOB random;
- one dominant target pattern:
  `movie_info(mi_movie_id) -> movie_info(mi_movie_id,mi_info_type_id)`;
- positive-arm replay from PR20d/PR20e;
- negative-control stress-subset replay from PR20f.

The design must not generalize this evidence to all workloads or all
prefix-subsuming composites without additional validation.

### R13. Hard Reject On Non-Positive What-if Signal

For the narrow prefix-upgrade proposal:

```text
C - {(T,c1)} + {(T,c1,c2)}
```

a non-positive workload-level what-if swap signal is a hard reject for future
online activation:

```text
whatif_gain <= 0 => ONLINE_REJECT
```

Here `whatif_gain` means the workload-level prefix-upgrade what-if signal used
by a future Gate B after that Gate B defines its aggregation window and
near-margin policy. It is not necessarily identical to PR20f Gate A's
`target_swap_whatif_rel_improvement`, which was a scalar offline stress-test
metric.

This rule is an online-activation eligibility veto, not a claim that the
upgrade is truly harmful and not a rule for suppressing offline/shadow logging.
If real-execution replay or shadow-observed evidence conflicts with a
non-positive what-if signal, the conflict must remain available for offline or
shadow analysis, but the proposal is not eligible for online activation until a
future validated conflict-resolution rule exists.

Near-zero or sign-unstable cases remain governed by R11: they should be treated
as shadow/deferred diagnostics and are not eligible for online swap. Positive
what-if gain only permits the proposal to enter shadow/deferred evidence
collection. It does not authorize an online swap.

## Design Influence From Online Physical Design Tuning

This section records design-only requirements and rationale for a future Gate B.
These requirements constrain future design review, but they do not unblock
PR21b-online, do not authorize runtime changes, and do not import a full online
tuning algorithm.

### Bruno-Chaudhuri: configuration transitions and incumbent evidence

PR21b treats prefix-upgrade as a configuration transition, not as a static
candidate-ranking correction. The narrow transition primitive remains:

```text
C - {(T,c1)} + {(T,c1,c2)}
```

A future Gate B should reason about accumulated evidence across workload
windows relative to transition cost, rather than a single-window what-if
snapshot. This is a representation requirement, not a formula or threshold
commitment. Delta, running-min, or running-max style evidence compression may
be useful inspiration, but this spec does not prescribe formulas, trigger
thresholds, or a binding NetBenefit equation.

Future Gate B evidence must separately represent:

- `AdmissionEvidence(u)`: evidence that the composite deserves entry;
- `RetentionEvidence(p)`: evidence that the incumbent prefix still deserves
  retention.

A composite candidate being visible is not evidence that the incumbent prefix
is safely droppable. If an incumbent prefix continues to be used or remains
beneficial for part of the workload, future validation must be able to observe
and record that evidence.

This is design-only. PR21b-design does not implement incumbent re-evaluation,
`U_keep`, `U_anchor`, or `_choose_config()` changes.

Any future online prefix-upgrade lane must include time-domain anti-churn
guards such as persistence, cooldown, and no-immediate-reversal. This
requirement addresses prefix/composite flip-flop under delayed evidence and
workload drift. It does not import Bruno-Chaudhuri-style storage-competition
residual ranking, because PR21b v1 is a narrow 1-for-1 prefix-upgrade lane, not
a general knapsack replacement search.

### DBA Bandits: prefix-aware representation and observed evidence

Future diagnostics must be prefix-position-aware. They must distinguish exact
leading-key preservation from mere column overlap. They must distinguish:

- `(T,c1,c2)` versus `(T,c2,c1)`;
- leading-key preservation versus same-column-set overlap;
- predicate key columns versus payload-only columns.

For the motivating example:

```text
movie_info(mi_movie_id, mi_info_type_id)
```

`mi_movie_id` remains the leading key and `mi_info_type_id` is the second key.

What-if remains diagnostic. Future Gate B validation must give higher
evidentiary weight to real-execution replay or shadow-observed behavior than
to pure what-if ordering. This does not authorize online exploration or
materialization in PR21b-design.

The spec does not claim that what-if is calibrated to real execution, nor that
what-if systematically underestimates or overestimates real execution.

Borrowing bandit-style design principles does not import regret bounds or
safety guarantees. Even future online exploration with regret guarantees would
not imply zero single-round regression. PR21b-online remains blocked until
AdaSelectPP-specific offline/shadow validation criteria pass.

## Future Online Gate B Requirements

A future Gate B design must first preserve the narrow operator boundary.

The operator action space is not the same as the set of possible decision
outcomes.

Operator action space:

```text
only C - {(T,c1)} + {(T,c1,c2)}
```

Decision outcomes may include:

- `REJECT`;
- `RETAIN_PREFIX`;
- `SHADOW_DEFER`;
- `FUTURE_HIGH_CONFIDENCE_ELIGIBLE`.

`keep both` may only be discussed as a future delayed-actuation or
build-before-drop transient state. It must not be treated as a general PR21b
action-space expansion.

No arbitrary drop-any/add-any swap is allowed. No non-prefix replacement is
allowed. No hidden candidate synthesis is allowed.

Before implementation, a future Gate B design should answer these questions:

1. How is the narrow prefix-to-composite operator recognized and bounded?
2. Which decision outcomes are allowed before any future online activation?
3. Which signals are required before a proposal can leave shadow/deferred
   state?
4. How is query-level concentration represented?
5. How are repeated small regressions balanced against rare large wins?
6. How are negative-control examples used to limit false accepts?
7. How are near-margin cases prevented from flipping into online churn?
8. How does the lane preserve atomicity under capacity pressure?
9. How does the lane interact with `appearing_curr`, `keep_visible`,
   `U_anchor`, and retain/swap visibility?
10. How are `AdmissionEvidence(u)` and `RetentionEvidence(p)` represented
    without implementing incumbent re-evaluation in this phase?
11. What storage and maintenance terms are required before net benefit can be
    called complete?
12. What artifact outputs are required to audit every accept, reject, shadow,
    and defer decision?

These questions are prerequisites for PR21b-online, not implementation notes for
the current phase.

## Diagnostic Artifact Requirements

Future offline or shadow-mode diagnostics should emit enough data to audit:

- candidate prefix and composite;
- prefix-position relationship, including leading-key preservation;
- old configuration;
- proposed replacement configuration;
- whether the prefix was selected, visible, or materialized;
- whether the composite was appearing, evaluated, visible, or materialized;
- what-if swap signal;
- real-execution replay signal when available;
- per-query benefit and regression distribution;
- round-level net benefit;
- concentration metrics;
- gate decision and all evidence used by the decision;
- negative-control category when applicable;
- near-margin marker;
- storage and maintenance proxy status;
- observed-evidence source, when available;
- explicit reason for accept, reject, shadow, or defer.

The artifact schema should make it possible to distinguish:

- positive-arm acceptance evidence;
- rejection-arm safety evidence;
- descriptive ordering diagnostics;
- storage/maintenance TODO fields.

## Out Of Scope For PR21b-Design

The following are out of scope for this phase:

- implementing PR21b online behavior;
- changing `_choose_config()`;
- changing selector logic;
- changing online policy;
- changing candidate generation;
- changing scoring or benefit logic;
- changing the evaluation-budget formula;
- changing `optimizer_ratio`;
- changing materialization policy;
- changing cooldown, payback, overlay, beta, or DML runtime behavior;
- reopening PR20g;
- adding candidate-generation experiments;
- introducing MAB/RL implementation;
- introducing online exploration;
- adding new runtime trace fields in code;
- adding tests that alter behavior;
- adopting UCB or acquisition priority inside candidate generation;
- adopting cooldown/carry/drift semantics;
- adopting benefit-threshold prefix domination;
- adopting predictive workload synthesis;
- width-3 expansion;
- joint candidate-generation and selector optimization.

## Acceptance Criteria For This Spec

This PR21b-design artifact is complete when it:

- records the Phase 0.5 milestone and official `probe_grow_fair` mode;
- explains why PR21b-design is justified and PR21b-online remains blocked;
- distinguishes PR20f Gate A from any future Gate B;
- states that scalar what-if threshold gating is insufficient;
- captures the dual-mode prefix-swap mechanism;
- requires workload-level net benefit and query-level concentration in future
  design work;
- preserves all runtime invariants and hard constraints;
- avoids claims of proof or what-if calibration direction.

This amendment is complete when:

- only documentation is changed;
- PR21b-online is still explicitly blocked;
- the narrow operator action space is clearly distinguished from decision
  outcomes;
- non-positive workload-level `whatif_gain` is a hard reject for future online
  activation, while conflicting real/shadow evidence may still be retained for
  offline or shadow analysis;
- positive what-if gain is not an accept rule;
- prefix-position-aware diagnostics are required;
- cross-window cumulative evidence is required as a future representation
  principle, without formulas or thresholds;
- incumbent retention evidence is required as a future design requirement;
- real/shadow-observed evidence is given higher evidentiary weight than pure
  what-if;
- no bandit regret or safety guarantee is imported;
- anti-churn is required, while Bruno-Chaudhuri-style storage-competition
  residual ranking is explicitly excluded;
- no runtime code, tests, selector, generator, budget, scoring,
  `optimizer_ratio`, or materialization behavior is changed.

## Current Conclusion

PR21b-design/spec is justified.

PR21b-online remains blocked.

The next valid step is review of the design requirements above, not runtime
implementation.
