# Phase 0.5 Candidate Generation

Phase 0.5 names the AdaSelect++ candidate-generation path as
`probe_grow_fair`.

The scope is deliberately narrow:

```text
K_k = G(phi_k, H_k)
```

That is, Phase 0.5 decides which index candidates are supplied to the online
tuner. It does not redefine evaluation (`E_k`), materialization state
transitions, the replacement overlay, the beta switch gate, or final
configuration selection (`s_k`).

## From LiteSelect To AdaSelect++

LiteSelect is an online single-column tuner. Its candidate universe is mostly
width-1 indexes, which keeps candidate supply simple but misses structural
multi-column opportunities in TPC-H/TPC-H-Skew style workloads.

AdaSelect++ Phase 0.5 extends candidate generation to bounded width-2 synthesis.
The key constraint is that naive width-2 enumeration is infeasible: enumerating
all ordered column pairs would inflate the candidate universe and evaluation
cost before the tuner has enough online evidence to rank them.

## Probe-Grow-Fair

`probe_grow_fair` is the canonical name for the bounded width-2 fairness
candidate-generation path. `PAIR_SUPPLY_FAIRNESS=1` and
`--pair_supply_fairness_enabled 1` remain backward-compatible aliases; when
they are used, the effective candidate generation mode is reported as
`probe_grow_fair`.

`probe_grow_fair` combines
the existing pieces that were developed through the Phase 0.5 PR sequence:

- single-column probe rounds;
- mature single-column seeds;
- bounded width-2 grow from static SQL evidence;
- per-table width-2 reserve;
- round-level width-2 reserve;
- unordered column-set diversity so both permutations do not consume reserve;
- diagnostic/expected structural pair type ranking for reserve ordering.

The grow step is seed based. It does not restore static width-2 enumeration.
Width remains bounded by `max_width=2`.

## Why Fair Supply Exists

Single-first grow alone has blind spots. A structural pair can be generated from
evidence but still be starved by width-first caps, especially when many
single-column candidates rank ahead of every width-2 candidate.

The fairness reserve is a candidate-supply mechanism for that situation. It
keeps total and per-table caps bounded by displacing same-table width-1
candidates when admitting selected width-2 pairs. It is a bounded recovery path,
not an unbounded expansion.

The `cg_*` metrics are the normalized Phase 0.5 candidate-generation view:

- `cg_width2_pre_cap_count`
- `cg_width2_post_cap_count`
- `cg_width2_dropped_round_count`
- `cg_width2_fairness_added_count`
- `cg_width2_fairness_added_pairs`
- `cg_target_pair_postround_coverage_count`
- `cg_candidate_budget_delta`

The older `width2_*` and `pair_supply_*` metrics remain for backward
compatibility with existing artifacts.

## Out Of Scope

The following are intentionally outside `probe_grow_fair`:

- what-if evaluation policy;
- fairness evaluation lane behavior;
- replacement overlay behavior;
- materialization bridge logic;
- beta/switch policy;
- timeout policy;
- benefit normalization;
- whitelist or create-time files;
- Two-CELF, MCTS, DML, U_keep, retain/swap, bandit/RL, or feedback loops.

Those belong to later phases or separate diagnostic PRs. Phase 0.5 candidate
generation ends at supplying a bounded candidate set.
