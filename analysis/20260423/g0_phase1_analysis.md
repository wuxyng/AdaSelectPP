# runs_g0_phase1 analysis

This analysis compares `g0_phase1_curr_only` vs `g0_phase1_mainline` from `runs_g0_phase1.zip`.

## Headline findings

- `U_keep` is active in `g0_phase1_mainline`: `selected_keep` and `keep_only_union` are non-zero in all four workloads, especially on `tpchs_noisy` and `tpchs_shifting`.

- Even so, **configuration trajectories are almost identical on tpchs**. Valid rounds with identical `new` configs: `tpchs_random 25/25`, `tpchs_noisy 96/96`, `tpchs_shifting 80/80`.

- Therefore the 4–5% worse `exec/total` on tpchs mainline is **not explainable by a different chosen configuration**; it is run-to-run noise / DB-side variance, not a policy difference.

- The only workload with a real policy change is `tpch_random`, where `mainline` replaces `orders(o_custkey)` with `lineitem(l_receiptdate)` in late rounds and slightly improves `total_avg` (-0.85%).

- On `tpchs_random`, `mainline` reduces `cand_sum` by 30 and `what_if_sum` by 20, but final configs remain the same. This means `U_keep` is changing the **evaluation path**, not the final decisions.

- On `tpchs_noisy` and `tpchs_shifting`, `selected_keep` is often positive, but `cand_sum`, `eval_sum`, and final configs are unchanged. `U_keep` is currently mostly a **visibility/state-continuity mechanism**, not a source of different chosen actions.


## Summary deltas

| bench   | wtype    |   rounds_curr_only |   rounds_mainline |   rounds_delta |   rounds_pct |   total_avg_curr_only |   total_avg_mainline |   total_avg_delta |   total_avg_pct |   exec_avg_curr_only |   exec_avg_mainline |   exec_avg_delta |   exec_avg_pct |   rec_avg_curr_only |   rec_avg_mainline |   rec_avg_delta |   rec_avg_pct |   trans_avg_curr_only |   trans_avg_mainline |   trans_avg_delta |   trans_avg_pct |   timeouts_curr_only |   timeouts_mainline |   timeouts_delta |   timeouts_pct |   what_if_sum_curr_only |   what_if_sum_mainline |   what_if_sum_delta |   what_if_sum_pct |   cand_sum_curr_only |   cand_sum_mainline |   cand_sum_delta |   cand_sum_pct |   eval_sum_curr_only |   eval_sum_mainline |   eval_sum_delta |   eval_sum_pct |   osc_avg_curr_only |   osc_avg_mainline |   osc_avg_delta |   stability_end_curr_only |   stability_end_mainline |   stability_end_delta |   stability_end_pct |
|:--------|:---------|-------------------:|------------------:|---------------:|-------------:|----------------------:|---------------------:|------------------:|----------------:|---------------------:|--------------------:|-----------------:|---------------:|--------------------:|-------------------:|----------------:|--------------:|----------------------:|---------------------:|------------------:|----------------:|---------------------:|--------------------:|-----------------:|---------------:|------------------------:|-----------------------:|--------------------:|------------------:|---------------------:|--------------------:|-----------------:|---------------:|---------------------:|--------------------:|-----------------:|---------------:|--------------------:|-------------------:|----------------:|--------------------------:|-------------------------:|----------------------:|--------------------:|
| tpch    | random   |                 25 |                25 |              0 |            0 |              16465.6  |             16326.1  |         -139.519  |       -0.847339 |             15147    |            15029.2  |        -117.71   |       -0.77712 |            145.492  |           144.971  |       -0.520913 |     -0.358036 |              1173.15  |             1151.86  |         -21.2884  |        -1.81464 |                    1 |                   1 |                0 |              0 |                    1076 |                   1059 |                 -17 |          -1.57993 |                  389 |                 355 |              -34 |       -8.74036 |                  346 |                 343 |               -3 |      -0.867052 |                   0 |                  0 |               0 |                  0.992    |                 1        |                 0.008 |            0.806452 |
| tpchs   | noisy    |                 96 |                96 |              0 |            0 |               1716.3  |              1793.65 |           77.3526 |        4.50694  |              1476.33 |             1546.41 |          70.081  |        4.74698 |             52.7404 |            52.6123 |       -0.128123 |     -0.242931 |               187.231 |              194.631 |           7.39973 |         3.95219 |                    0 |                   0 |                0 |            nan |                    1010 |                   1010 |                   0 |           0       |                 1027 |                1027 |                0 |        0       |                  600 |                 600 |                0 |       0        |                   0 |                  0 |               0 |                  0.997917 |                 0.997917 |                 0     |            0        |
| tpchs   | random   |                 25 |                25 |              0 |            0 |              10217.8  |             10655.4  |          437.608  |        4.2828   |              9042.74 |             9444.35 |         401.611  |        4.44125 |            149.653  |           150.247  |        0.594345 |      0.397149 |              1025.41  |             1060.81  |          35.4026  |         3.45255 |                    0 |                   0 |                0 |            nan |                    1081 |                   1061 |                 -20 |          -1.85014 |                  383 |                 353 |              -30 |       -7.8329  |                  348 |                 342 |               -6 |      -1.72414  |                   0 |                  0 |               0 |                  1        |                 1        |                 0     |            0        |
| tpchs   | shifting |                 80 |                80 |              0 |            0 |               1795.85 |              1873.66 |           77.8137 |        4.33298  |              1520.32 |             1589.05 |          68.7284 |        4.52064 |             52.7207 |            52.3792 |       -0.341512 |     -0.647775 |               222.804 |              232.23  |           9.4268  |         4.23099 |                    0 |                   0 |                0 |            nan |                     842 |                    842 |                   0 |           0       |                  860 |                 860 |                0 |        0       |                  502 |                 502 |                0 |       0        |                   0 |                  0 |               0 |                  0.9975   |                 0.9975   |                 0     |            0        |

## Config trajectory identity

| bench   | wtype    |   valid_rounds |   same_new_rounds |   same_new_frac |   same_old_rounds |   same_old_frac |
|:--------|:---------|---------------:|------------------:|----------------:|------------------:|----------------:|
| tpch    | random   |             25 |                19 |            0.76 |                20 |             0.8 |
| tpchs   | random   |             25 |                25 |            1    |                25 |             1   |
| tpchs   | noisy    |             96 |                96 |            1    |                96 |             1   |
| tpchs   | shifting |             80 |                80 |            1    |                80 |             1   |

## Keep-lane activity in mainline

| bench   | wtype    |   selected_keep_sum |   selected_keep_max |   rounds_selected_keep_gt0 |   keep_only_union_sum |   keep_only_union_max |   corr_trials_sum |   old_rel_not_app_sum |
|:--------|:---------|--------------------:|--------------------:|---------------------------:|----------------------:|----------------------:|------------------:|----------------------:|
| tpch    | random   |                  38 |                   2 |                         22 |                    58 |                     5 |                23 |                    23 |
| tpchs   | random   |                  36 |                   2 |                         21 |                    68 |                     6 |                24 |                    24 |
| tpchs   | noisy    |                 323 |                   7 |                         75 |                   438 |                     7 |                 0 |                     0 |
| tpchs   | shifting |                 260 |                   6 |                         60 |                   360 |                     7 |                 0 |                     0 |

## Interpretation

1. **Phase-1 mainline is wired in**: `selected_keep > 0` and `keep_only_union > 0` prove that `U_keep` entered the visible action universe.

2. **But it rarely changes final decisions on tpchs**: since `new` is identical round-by-round, `U_keep` is not yet strong enough (or not yet placed early enough in scoring/selection) to alter chosen configs there.

3. **Mainline is not hurting tpchs through different configs**: the tpchs slowdown is not policy-caused by different indexes, because the indexes are the same.

4. **tpch_random is the only real signal**: there, `U_keep`/state-aware visibility slightly changes late-round choice and modestly improves total time.


## What this means for the architecture

- The `U_keep` idea is no longer absent; it is present and measurable.

- However, Phase 1 currently acts more like **state continuity / anti-forgetting insurance** than a strong decision-changing lane.

- The next useful step is not to revisit legacy logic, but to decide whether `KEEP-only` should remain a weak backfill lane or be given a slightly stronger persistence term once the current spine is stable.


## Crucial architecture insight
- In the current cleaned Phase-1 code, `_choose_config()` uses `visible_action = appearing_curr ∪ old_conf` for **both** variants.
- Therefore `U_keep` in `g0_phase1_mainline` does **not** expand the final action space beyond what `old_conf` already provides.
- Its only live effect is on the **benefit-estimation path**: keeping incumbents visible, avoiding decay / NO_HIT treatment, and occasionally triggering correctness evaluation.
- This explains why tpchs shows substantial `selected_keep` activity but almost no change in final `new` configurations.
