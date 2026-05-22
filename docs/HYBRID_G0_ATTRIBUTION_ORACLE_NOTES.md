# Hybrid-G0 Attribution + Restricted Oracle Patch

Base: `repo60408_g0_ranker_redesign.zip`.

This patch keeps the old G0/ranker-redesign timeout policy and does **not** change the main algorithmic behavior by default, except for renaming fallback evidence in metadata and adding diagnostic/ablation plumbing.

## Mainline timeout policy

Use the original G0/ranker-redesign timeout policy for mainline reproduction. The newer annotation-style timeout policy should only be tested as a separate ablation after the baseline is stable.

## Added attribution

Per-round CSV now exposes:

- `graph_rich_query_count`
- `graph_sparse_query_count`
- `role_fallback_query_count`
- `fallback_candidate_count`
- `fallback_selected_count`
- `legacy_supplement_candidate_count`
- `legacy_supplement_selected_count`
- `oldconf_refresh_added`
- `oldconf_refresh_queries`
- `oldconf_refresh_unique_indexes`
- `oldconf_refresh_max_aff`
- `oldconf_refresh_enabled`
- `oldconf_refresh_positive_only`
- `oldconf_refresh_predicate_only`
- `oldconf_refresh_max_queries_per_index`

`SEL_FALLBACK` has been renamed in emitted metadata to `SEL_ROLE_FALLBACK` to make clear that it is bounded role-summary evidence, not graph/factor structured evidence.

## Added ablation knobs

Defaults preserve the G0/ranker-redesign behavior:

```json
"g0_role_fallback_enabled": true,
"g0_legacy_supplement_enabled": true,
"g0_oldconf_refresh_enabled": true,
"g0_oldconf_refresh_max_queries_per_index": 0,
"g0_oldconf_refresh_positive_only": false,
"g0_oldconf_refresh_predicate_only": false
```

New configs:

- `adasel/config/adaselect_hybrid_g0_baseline.json`
- `adasel/config/adaselect_hybrid_g0_no_role_fallback.json`
- `adasel/config/adaselect_hybrid_g0_refresh_cap3.json`
- `adasel/config/adaselect_hybrid_g0_refresh_positive_only.json`
- `adasel/config/adaselect_hybrid_g0_refresh_predicate_only.json`

## Affinity diagnostic fix

The high-affinity threshold now uses `ceil(0.8 * round_size)` rather than `round(0.8 * round_size)`, and the warning has been downgraded to an info-level diagnostic. This prevents JOB round-size 8 from treating 6/8 as a misleading `>=0.80` warning.

## Scripts

Run attribution/ablation:

```bash
bash scripts/run_hybrid_g0_attribution_ablation.sh
```

Collect attribution summary:

```bash
python analysis/hindsight_oracle/collect_hybrid_attribution.py \
  --runs-root runs_hybrid_g0_attribution \
  --out runs_hybrid_g0_attribution/attribution_summary.csv
```

Run first-pass observed-config oracle:

```bash
bash scripts/run_hybrid_g0_oracle_firstpass.sh runs_hybrid_g0_attribution oracle_firstpass
```

The oracle script is a first-pass observed-config diagnostic. It uses observed rows/configs and does not yet do physical replay or full cost-matrix estimation.
