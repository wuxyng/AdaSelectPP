# Evaluation Budget Semantics

PR20a is instrumentation only. It exposes how AdaSelect++ computes the
per-round what-if evaluation budget under bounded candidate generation, but it
does not change online policy decisions, candidate generation, selector logic,
materialization, overlay behavior, beta, cooldown, payback, or budget formulas.

## Why This Is Logged

Historically, `optimizer_ratio` was meaningful when the candidate universe was
exhaustive or a large static set. In that setting, multiplying by the candidate
count gave a rough proportional what-if budget over a broad action universe.

In Phase 0.5, `probe_grow` and `probe_grow_fair` use bounded candidate
generation. The current evaluation budget is computed from the post-cap
`appearing` candidate set:

- first round: `eval_budget = appearing_count`
- later rounds: `eval_budget = max(1, int(optimizer_ratio * appearing_count))`

That means benchmark-specific `optimizer_ratio` values now scale the budget
after generator caps have already constrained the pool. For JOB in particular,
raw candidate evidence can be richer than the post-cap appearing set, while a
smaller ratio still reduces the absolute number of candidates that enter
what-if evaluation.

## Trace Fields

PR20a adds per-round trace fields for the budget audit:

- `candidate_count_raw`
- `appearing_count`
- `candidate_topk`
- `optimizer_ratio`
- `eval_budget_formula`
- `eval_budget`
- `evaluated_count`
- `budgeted_out_count`
- `width1_appearing_count`
- `width2_appearing_count`
- `width1_evaluated_count`
- `width2_evaluated_count`
- `width2_eval_coverage_ratio`
- `structural_pair_eval_budgeted_out_count`
- `fairness_eval_lane_budgeted_out_count`
- `materialization_gap_eval_gap`

`candidate_count_raw`, structural-pair, fairness-lane, and materialization-gap
fields are best-effort diagnostics when the underlying instrumentation is
available.

## Non-Goals

This PR does not retune `optimizer_ratio`, alter the budget formula, add a
fallback generator, change the online selector, or increase materialization.
The purpose is to make the current budget semantics visible so later policy
work can be motivated by trace evidence rather than inference.
