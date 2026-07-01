# Evidence for Prefix-Swap Opportunity

This report summarizes the Phase 0.5 evidence chain for prefix-swap behavior.
It is documentation only. It does not change online policy, selector logic,
candidate generation, scoring, evaluation budget, optimizer ratio, or
materialization behavior.

## Milestone Status

Phase 0.5 is closed at merge commit:

```text
1a1129ad3b2dc512a3951aca9cdada97d966e774
```

Official candidate-generation mode:

```text
candidate_generation_mode = probe_grow_fair
```

Next phase:

```text
PR21b-design/spec only
```

Blocked:

```text
PR21b-online
```

## Evidence Chain

PR19 validated candidate-pool quality for `probe_grow_fair` under a fixed
offline pool-restricted CELF oracle. The result supported the candidate
generation change as a pool-quality improvement, not an online-policy change.

PR20a added evaluation-budget audit instrumentation. The JOB audit showed that
JOB has denser raw and appearing width-2 candidate supply than TPCH/TPCHS, while
width-2 evaluation coverage remains low under the current bounded evaluation
path.

PR20b tested whether increasing JOB `optimizer_ratio` from the historical value
was sufficient. The diagnostic increased evaluation budget and what-if calls,
but selected width-2 remained zero and observed workload cost did not improve.
That made ratio-only tuning insufficient as an explanation or remedy.

PR20c added an offline what-if oracle for width-2 add/swap configurations. It
found prefix-swap opportunity on JOB, especially replacement value where a
selected width-1 prefix index is replaced by a width-2 composite index.

PR20d and PR20e added physical replay diagnostics for the dominant target
pattern:

```text
movie_info(mi_movie_id)
->
movie_info(mi_movie_id,mi_info_type_id)
```

Those runs provided real-execution positive-arm evidence: when the dominant
movie_info target pattern appeared in the PR20c target-best set, replay often
showed positive workload-level execution impact. The evidence is strongest as
an existence signal for this one target pattern, not as a complete online
selector rule.

PR20f added negative-control replay for the same target pattern. It tested
non-target-best, predicted-low, predicted-negative, and near-margin rounds under
simple offline gate thresholds. The result is a rejection-arm warning: a simple
scalar threshold over target what-if relative improvement is not safe enough for
online activation. In the stress subset, lower thresholds allowed too many
flat/worse swaps, while higher thresholds rejected too many real-execution wins.

## Interpretation

The Phase 0.5 evidence now separates three issues:

- Candidate generation is no longer the bottleneck for the observed JOB
  prefix-swap question.
- There is credible evidence for a prefix-swap opportunity in the dominant
  movie_info pattern.
- A simple scalar what-if threshold is not a sufficient online gate for that
  opportunity.

The PR20f result should be read as a design constraint. It does not say that
prefix swaps are low-value. It says that the online design cannot rely on a
single scalar what-if threshold to decide accept/reject behavior.

## Relation to Broader Candidate-Generation Ideas

The external AdaSelectPP idea review contains several candidate-generation
ideas that are useful only if kept selector-agnostic in the current phase.

Predictive recall is useful as an offline reporting metric under delayed
actuation. Candidate generation for round `k` should eventually be reported
against opportunities in `W_{k+1}`, not only `W_k`, because indexes selected
after observing one round affect later execution. This is a future reporting
metric, not a predictive candidate synthesis module.

Static structural pair evidence is also plausible as a future width-2
candidate-generation lane. Examples include join evidence, co-access evidence,
group evidence, and order evidence. Such a lane must remain selector-agnostic
and width-2 bounded. It is not a current blocker because PR19 through PR20f show
that the immediate bottleneck has shifted from candidate supply to
configuration-level prefix-swap selection.

Lane fairness has already been absorbed by `probe_grow_fair`. In Phase 0.5,
fairness means bounded-pool reallocation, not budget expansion.

The current phase should not adopt:

- UCB or acquisition priority inside candidate generation;
- cooldown, carry, or drift semantics;
- benefit-threshold prefix domination;
- predictive workload synthesis;
- width-3 expansion;
- joint candidate-generation and selector optimization.

These mechanisms mix candidate generation with selector or policy
responsibilities, or they reopen frozen Phase 2 scope. They should remain out of
the Phase 0.5 findings closure.

## Limitations

- The replay evidence is from a single benchmark/workload: JOB random.
- The physical replay evidence focuses on a single dominant target pattern:
  `movie_info(mi_movie_id) -> movie_info(mi_movie_id,mi_info_type_id)`.
- PR20e validates the positive arm only.
- PR20f is a negative-control stress subset, not a global ROC estimate for all
  possible prefix swaps.
- Ordering diagnostics are descriptive only, not calibration.
- This report does not claim that what-if underestimates or overestimates real
  execution.
- Near-margin `improved` / `flat` / `worse` labels are fragile across replay
  runs and should be interpreted as approximate evidence, not exact labels.

## Conclusion

PR21b-online remains blocked.

PR21b-design/spec is justified.

Candidate generation is no longer the bottleneck.
