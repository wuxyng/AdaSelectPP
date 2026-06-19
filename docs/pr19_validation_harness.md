# PR19 Offline Candidate-Pool Validation Harness

PR19 compares candidate-pool quality only. It does not change AdaSelectPP online
execution, selector policy, materialization, overlay behavior, beta gates,
cooldown, payback, or online feedback logic.

## What PR19 Compares

The harness exports per-round candidate pools for the same benchmark workload
under two Phase 0.5 candidate-generation modes:

```bash
candidate_generation_mode=probe_grow
candidate_generation_mode=probe_grow_fair
```

`probe_grow_fair` is expected to address bounded width-2 round-cap starvation.
The important question for PR19 is whether the recovered width-2 supply creates
a better downstream action space when the selector is fixed.

## Harness Flow

1. `tools/export_candidate_pools.py`
   - Loads workloads through the existing `adasel.main.load_workloads` helper.
   - Runs `MCIGCandidateGenerator.generate(...)` twice per round with identical
     offline seed state and config, except for `candidate_generation_mode`.
   - Writes:

```text
runs_pr19_candidate_pool/<bench>_<wtype>/<mode>/candidate_pools.jsonl
```

2. `tools/offline_validate_candidate_pool.py`
   - Reads the exported JSONL.
   - Uses `offline_pool_celf`, a pool-restricted offline CELF oracle, over the
     exported pool.
   - Uses the existing HypoPG / `CostEvaluation.calculate_now_cost` path.
   - Creates virtual indexes only; no physical indexes are created.
   - Writes:

```text
runs_pr19_candidate_pool/<bench>_<wtype>/<mode>/offline_validation.csv
```

3. `tools/analyze_pr19.py`
   - Joins `probe_grow` and `probe_grow_fair` validation rows by
     `bench`, `workload_type`, and `round_id`.
   - Writes:

```text
runs_pr19_candidate_pool/<bench>_<wtype>/pr19_summary.csv
runs_pr19_candidate_pool/<bench>_<wtype>/pr19_round_deltas.csv
```

## Example

```bash
python tools/export_candidate_pools.py tpchs random --round-size 50
python tools/offline_validate_candidate_pool.py tpchs random --round-size 50 --max-num 10
python tools/analyze_pr19.py tpchs random
```

The fixed downstream selector isolates candidate-pool quality. A fair win means
`probe_grow_fair` produced an exported pool that let the same offline oracle
reach a larger relative cost improvement than `probe_grow` for that round.

Each `offline_validation.csv` row records the validator metadata:

```text
selector_name=offline_pool_celf
selector_semantics=pool_restricted_deterministic
liteselect_twocelf_imported=false
```

## Selector Choice

PR19 uses `offline_pool_celf` as the primary downstream selector. It is a
pool-restricted deterministic offline oracle: it consumes only the exported
candidate strings in `candidate_pools.jsonl`; it does not call AdaSelectPP
candidate generation internally.

PR19 does not import or reproduce the older LiteSelect two-CELF/RPA online
implementation.

The LiteSelect two-CELF/RPA implementation is useful design context, but it is
not a clean PR19 dependency because it is an online state machine with add/drop
heaps, cooldown-driven online rejection, physical transition logic, and
reactive parameter adaptation. Two details are especially important for PR19:

- Its capacity-swap fallback commits DROP before ADD. If the paired ADD is
  rejected by cooldown, the swap becomes non-atomic.
- Its RPA update can overwrite the configured creation penalty/beta, which
  changes fixed benchmark/workload parameter semantics during the run.

Those behaviors would confound an offline candidate-pool comparison. A future
PR19b may add a frozen LiteSelect adapter, but only after disabling RPA,
physical materialization, cooldown-driven online rejection, and making capacity
swap atomic.

## Explicit Non-Goals

PR19 does not add or modify:

- online AdaSelectPP behavior;
- selector or materialization policy;
- replacement overlay;
- beta, cooldown, payback, or online feedback policy;
- fallback candidate generation;
- compile validation;
- physical index materialization.
