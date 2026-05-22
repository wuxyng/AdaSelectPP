# Phase 1 vs previous G0-3/ranker result analysis

## Key numeric comparison: current `curr_keep_topk_beta` vs previous `summary-G0-3`

| case           |   total_pct |   exec_pct |   rec_pct |   trans_pct |   cand_pct |   eval_pct |   whatif_pct |   whatif_per_eval_g03 |   whatif_per_eval_new |
|:---------------|------------:|-----------:|----------:|------------:|-----------:|-----------:|-------------:|----------------------:|----------------------:|
| job_noisy      |       -5.86 |     -31.06 |     88.82 |      390.82 |      10.42 |     169.44 |        16.15 |                  4.49 |                  1.93 |
| job_random     |       28.89 |      14.35 |     22.37 |      113.06 |      -2.9  |     126.87 |       -32.33 |                 13.98 |                  4.17 |
| job_shifting   |       -4.52 |     -29.33 |     99.99 |      113.73 |       9.38 |     173.19 |        21.1  |                  4.4  |                  1.95 |
| tpch_noisy     |       10.29 |       7.37 |     11.6  |       62.29 |       4.07 |      24.07 |        -6.42 |                  2.18 |                  1.64 |
| tpch_random    |        2.6  |      -1.77 |     15.05 |      131.43 |     -37.28 |      20.77 |       -16.18 |                  4.46 |                  3.1  |
| tpch_shifting  |       11.1  |       7.59 |      8.11 |       64.58 |       4.55 |      24.38 |        -0.36 |                  2.04 |                  1.64 |
| tpchs_noisy    |        5.91 |       0.83 |     12.66 |       68.66 |     -10.85 |       3.09 |       -16.87 |                  2.09 |                  1.68 |
| tpchs_random   |        7.18 |      -2.31 |     18.78 |      476.58 |     -44.93 |      -2.01 |       -17.33 |                  3.67 |                  3.1  |
| tpchs_shifting |        7.9  |       0.71 |     13.97 |      100.83 |     -10.42 |       3.29 |       -12.2  |                  1.97 |                  1.68 |

## Main conclusions

1. Regression is not caused only by retain/swap. `curr_keep_topk_beta` also regresses against the previous G0-3 summary in most TPCH/TPCHS cases and especially in JOB random.

2. JOB random is the strongest failure: total +28.9%, exec +14.3%, transition +113.1%, even under topk_beta. Retain/swap makes it worse, but topk_beta itself is already behind the previous design.

3. The new Phase-1 path changed the evaluation semantics: compared with previous G0-3, it evaluates more candidate keys but each candidate is tested on far fewer relevant queries. For JOB random, what-if per evaluated candidate drops from 13.97 to 4.17. This suggests a relevance-map narrowing / under-evaluation issue.

4. Previous G0-3 generator built `query_indexes` by adding old_conf indexes to per-query relevance if the leading column matched query roles. Phase 1 query_indexes is only U_curr; keep_visible is not tested. This likely weakens incumbent benefit refresh and candidate benefit estimates, especially in JOB.

5. Old `_choose_config` selected from the whole `columns_benefit` map; Phase 1 topk_beta restricts visible action space to `appearing_curr ∪ old_conf`. This removed an implicit history-anchor action space. U_keep only preserves current installed indexes, not previously-good-but-dropped indexes.
