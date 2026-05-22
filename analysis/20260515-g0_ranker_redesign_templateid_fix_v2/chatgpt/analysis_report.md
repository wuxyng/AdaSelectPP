# 20260515 template-id/plan-cache fix v2 result analysis

## Key conclusion

- The run is **not a reproduction** of the previous G0/ranker-redesign baseline. The template-id / exact-SQL plan-cache fix changed the evidence path and greatly expanded the evaluated candidate pool.

- The fix is conceptually necessary because the old q0/q1 template-cache reuse was unsafe, but the current post-fix system is **too broad**: candidate/eval/what-if increased sharply, especially on `job_random` and `tpchs_random`.

- `supplier(s_nationkey)` is back, but the system also admits extra candidates such as `orders(o_custkey)`, `partsupp(ps_supplycost)`, `part(p_brand/p_container)`, causing larger recommendation cost and, for `tpchs_random`, worse execution and a timeout.

## Adaptive WDCG=1 vs previous G0/ranker-redesign

| bench   | wtype   |   total_avg_pct |   exec_avg_pct |   rec_avg_pct |   trans_avg_pct |   what_if_sum_pct |   cand_sum_pct |   eval_sum_pct |
|:--------|:--------|----------------:|---------------:|--------------:|----------------:|------------------:|---------------:|---------------:|
| job     | random  |           69.26 |          13.15 |        317.42 |           77.00 |            159.88 |         159.90 |         161.88 |
| tpchs   | noisy   |            2.99 |           2.67 |         12.38 |            2.85 |             25.27 |          69.10 |          79.90 |
| tpchs   | random  |           56.47 |          56.57 |         62.10 |           47.74 |             45.20 |          50.86 |          49.00 |

## Current WDCG=1 vs WDCG=0

| bench   | wtype   |   total_avg_pct |   exec_avg_pct |   rec_avg_pct |   trans_avg_pct |   what_if_sum_pct |   cand_sum_pct |   eval_sum_pct |
|:--------|:--------|----------------:|---------------:|--------------:|----------------:|------------------:|---------------:|---------------:|
| job     | random  |          -22.74 |           5.59 |        -35.38 |          -43.99 |            -37.79 |         -52.23 |         -52.55 |
| tpchs   | noisy   |          -26.53 |         -20.33 |        -43.97 |          -59.81 |            -77.52 |         -80.96 |         -79.62 |
| tpchs   | random  |           21.27 |          34.31 |        -49.46 |          -76.23 |            -76.56 |         -80.84 |         -80.18 |

## Fixed vs adaptive under WDCG=1

| bench   | wtype   |   total_avg_pct |   exec_avg_pct |   rec_avg_pct |   trans_avg_pct |   what_if_sum_pct |   cand_sum_pct |   eval_sum_pct |
|:--------|:--------|----------------:|---------------:|--------------:|----------------:|------------------:|---------------:|---------------:|
| job     | random  |           -5.10 |          -5.85 |         -4.98 |           -2.87 |             -2.12 |           0.00 |           0.00 |
| tpchs   | noisy   |           29.98 |          10.42 |          4.76 |          288.58 |              7.56 |          -2.72 |           6.97 |
| tpchs   | random  |           -4.77 |          -5.15 |         -9.51 |           19.12 |             -0.38 |           0.00 |           0.19 |

## tpchs_noisy evolution across versions

| label               |   rounds |   cand_sum |   raw_sum |   eval_sum |   what_if_sum |   timeout |   exec_avg |   rec_avg |   trans_avg |   total_avg |
|:--------------------|---------:|-----------:|----------:|-----------:|--------------:|----------:|-----------:|----------:|------------:|------------:|
| 20260510            |       96 |    1152.00 |   1248.00 |     582.00 |       1215.00 |      0.00 |    1427.59 |     50.32 |      113.18 |     1591.08 |
| 20260513            |       96 |    1056.00 |   1152.00 |     486.00 |       1152.00 |      0.00 |    1566.56 |     45.80 |      108.88 |     1721.23 |
| 20260515_templateid |       96 |    1948.00 |   2006.00 |    1047.00 |       1522.00 |      0.00 |    1465.66 |     56.54 |      116.40 |     1638.61 |

## Interpretation

1. Previous G0/ranker-redesign good results were partly tied to unsafe plan reuse / narrower evidence. After fixing template IDs and disabling template/signature plan cache, candidate evidence is more truthful but much broader.

2. Broad evidence is not automatically better. The current rank/selector/budget was tuned under the narrower old evidence path. With exact-SQL evidence, it over-admits candidates, increasing what-if and rec cost, and on `tpchs_random` hurts execution.

3. `job_random` still improves over WDCG=0, but no longer matches the old G0/ranker baseline. It now has 1523 candidates and 419 evaluated vs old 586/160; rec is 4x old.

4. `tpchs_noisy` is closest to acceptable: supplier is restored and total is only about 3% above old, but candidate/eval are 69%/80% higher.

5. `tpchs_random` is the serious regression: WDCG=1 is worse than WDCG=0 and 56% worse than old G0/ranker, with one timeout.
