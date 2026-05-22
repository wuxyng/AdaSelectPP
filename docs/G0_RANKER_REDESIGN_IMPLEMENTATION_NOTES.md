# G0 Ranker Redesign: implementation notes

This patch replaces the legacy role-bag ranking semantics on the G0 path.

## What changed

### 1. New structured ranker

Added `StructuredWDCGRanker` in `adaselect_pp/core/ranker.py`.

It separates ranking into two channels:

- **base channel** for G0-2 selection prefixes (`SEL_EQ1`, `SEL_EQ_EQ`, `SEL_EQ_RANGE`, etc.)
- **merge channel** for G0-3 suffix candidates (`MERGE_GROUP`, `MERGE_ORDER`, `MERGE_COVER`)

Base keys are scored from:

- table scan impact
- table selectivity
- structured family prior
- extraction/evidence quality
- weak history/frequency priors
- risk penalty

Merge keys are scored as **marginal gain on top of a selected base key**, not as stand-alone indexes.

### 2. Two-stage top-k selector

Added `StructuredTopKSelector` in `adaselect_pp/core/generator.py`.

Selection now works as:

1. select base prefixes first
2. then attach a bounded number of merges
3. only allow merge if its `base_key` is already selected
4. at most `max_merges_per_base`

This removes the old failure mode where merge candidates competed in the same flat top-k pool and crowded out useful selection prefixes.

### 3. Structured metadata surfaced to the ranker

Base candidate metadata now carries `quality_score`.

Merge metadata now carries:

- `base_key`
- `suffix_col`
- `evidence_confidence`

This lets the ranker reason about marginal suffix value rather than reusing legacy role averaging.

### 4. Automatic wiring

`CandidateGenerator` now does:

- `legacy_cooc` -> old `WDCGRanker` + old `TopKSelector`
- `g0_selection` -> new `StructuredWDCGRanker` + new `StructuredTopKSelector`

No extra config switch is required.

## What did NOT change

- legacy `CooccurrenceEnumerator` path
- compile validation policy
- merge generation legality rules

This patch fixes the ranking semantics only.

## Validation

Passed:

- `test_structured_selection.py`
- `test_structured_merge.py`
- `test_structured_ranker.py`

Total: `12 passed`
