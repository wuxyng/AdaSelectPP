# Clean AdaSelect / MCIG-v1 Spine

This package intentionally resets the previous experimental branches.

Mainline:

```text
Static SQL evidence (MCIG-v1)
-> budgeted what-if benefit estimation
-> top-k proposal
-> workload-specific beta transition gate
```

Removed from the mainline:

```text
CooccurrenceEnumerator
legacy_cooc enum mode
G0-3 merge plumbing
compile validation hard gate
retain/swap
Phase1 / Phase1R U_keep/U_anchor split-visibility machinery
unsafe template/signature EXPLAIN plan cache
```

Key principles:

1. Candidate generation uses SQL text + schema + optional indexable-column whitelist only.
2. EXPLAIN is used only in benefit/cost evaluation, not to decide which columns exist as candidates.
3. Multi-column candidates follow B-tree shape: equality prefix + optional one range, width <= 2.
4. Fallback is bounded: static fallback / candidate-vacuum rescue, not old cooccurrence families.
5. `indexable_columns.txt` is a whitelist, not an active enumeration source.
6. `template_id\tSQL` workload lines are preserved internally; all DB calls strip template id before execution.

Important: this is a new clean baseline, not a drop-in attempt to reproduce older G0/ranker-redesign numbers.
Run first-pass validation on small cases before full sweeps.
