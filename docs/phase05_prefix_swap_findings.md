# Evidence for Prefix-Swap Opportunity

This report summarizes the Phase 0.5 evidence chain for prefix-swap behavior.
It is documentation only. It does not change online policy, selector logic,
candidate generation, scoring, evaluation budget, optimizer ratio, or
materialization behavior.

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
