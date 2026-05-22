# G0-3 ablation postmortem

## Headline

- A1/B1/C1 (with compile) regress mainly because compile validation rejects too many candidates and collapses the pool.

- B0/C0 (G0-3 without compile) regress mainly because merge introduces weak group/order-legacy candidates that displace useful G0-2 base keys.

- C0/C1 are effectively the same as B0/B1 in this run: merged_covering is zero, and no MERGE_COVER rows appear in trace.


## Evidence

| variant                   | bench   | wtype    |   rounds |   exec_avg |   total_avg |   timeouts |   cand_sum |   whatif_sum |   merged_total |   compile_trials |   compile_not_picked |   exec_delta_pct_vs_A0 |   total_delta_pct_vs_A0 |   timeouts_delta_vs_A0 |
|:--------------------------|:--------|:---------|---------:|-----------:|------------:|-----------:|-----------:|-------------:|---------------:|-----------------:|---------------------:|-----------------------:|------------------------:|-----------------------:|
| A0_g0_2_base_no_compile   | tpch    | random   |       25 |   15134.5  |    15738.8  |          1 |        566 |         1267 |              0 |                0 |                    0 |               0        |              0          |                      0 |
| A1_g0_2_base_with_compile | tpch    | random   |       25 |   15071.3  |    15739.2  |          1 |        145 |         1394 |              0 |              666 |                  521 |              -0.417573 |              0.00277276 |                      0 |
| B0_g0_3_go_no_compile     | tpch    | random   |       25 |   15177.6  |    15829.3  |          1 |       1174 |         1914 |            765 |                0 |                    0 |               0.285199 |              0.575006   |                      0 |
| B1_g0_3_go_with_compile   | tpch    | random   |       25 |   15011.9  |    15731.8  |          1 |        207 |         1464 |            765 |             1319 |                 1112 |              -0.810208 |             -0.0440076  |                      0 |
| C0_g0_3_goc_no_compile    | tpch    | random   |       25 |   15119.2  |    15773.5  |          1 |       1174 |         1914 |            765 |                0 |                    0 |              -0.100904 |              0.220942   |                      0 |
| C1_g0_3_goc_with_compile  | tpch    | random   |       25 |   15034.2  |    15757.8  |          1 |        207 |         1464 |            765 |             1319 |                 1112 |              -0.662294 |              0.121077   |                      0 |
| A0_g0_2_base_no_compile   | tpchs   | noisy    |       96 |    1428.63 |     1589.76 |          0 |       1152 |         1246 |              0 |                0 |                    0 |               0        |              0          |                      0 |
| A1_g0_2_base_with_compile | tpchs   | noisy    |       96 |    3311.99 |     3457.64 |          2 |        199 |         1359 |              0 |             1222 |                 1023 |             131.83     |            117.495      |                      2 |
| B0_g0_3_go_no_compile     | tpchs   | noisy    |       96 |    3304.07 |     3459.76 |          2 |       1692 |         1477 |            658 |                0 |                    0 |             131.276    |            117.628      |                      2 |
| B1_g0_3_go_with_compile   | tpchs   | noisy    |       96 |    3301.58 |     3465.83 |          2 |        205 |         1597 |            658 |             1786 |                 1581 |             131.102    |            118.01       |                      2 |
| C0_g0_3_goc_no_compile    | tpchs   | noisy    |       96 |    3305.47 |     3460.16 |          2 |       1692 |         1477 |            658 |                0 |                    0 |             131.374    |            117.654      |                      2 |
| C1_g0_3_goc_with_compile  | tpchs   | noisy    |       96 |    3309.26 |     3473.23 |          2 |        205 |         1597 |            658 |             1786 |                 1581 |             131.639    |            118.476      |                      2 |
| A0_g0_2_base_no_compile   | tpchs   | random   |       25 |    9025.25 |     9332.94 |          0 |        641 |         1281 |              0 |                0 |                    0 |               0        |              0          |                      0 |
| A1_g0_2_base_with_compile | tpchs   | random   |       25 |    9009.86 |     9506.59 |          0 |        180 |         1344 |              0 |              783 |                  603 |              -0.170588 |              1.86055    |                      0 |
| B0_g0_3_go_no_compile     | tpchs   | random   |       25 |    9040.26 |     9468.81 |          0 |       1198 |         1645 |            737 |                0 |                    0 |               0.166318 |              1.45575    |                      0 |
| B1_g0_3_go_with_compile   | tpchs   | random   |       25 |    9003.98 |     9560.56 |          0 |        247 |         1417 |            737 |             1385 |                 1138 |              -0.235747 |              2.43882    |                      0 |
| C0_g0_3_goc_no_compile    | tpchs   | random   |       25 |    8970.5  |     9403.75 |          0 |       1198 |         1645 |            737 |                0 |                    0 |              -0.606701 |              0.758683   |                      0 |
| C1_g0_3_goc_with_compile  | tpchs   | random   |       25 |    9022.55 |     9561.11 |          0 |        247 |         1417 |            737 |             1385 |                 1138 |              -0.029913 |              2.44476    |                      0 |
| A0_g0_2_base_no_compile   | tpchs   | shifting |       80 |    1482.23 |     1641.8  |          0 |        960 |          978 |              0 |                0 |                    0 |               0        |              0          |                      0 |
| A1_g0_2_base_with_compile | tpchs   | shifting |       80 |    3691.2  |     3815.37 |          3 |        166 |          485 |              0 |             1014 |                  848 |             149.03     |            132.389      |                      3 |
| B0_g0_3_go_no_compile     | tpchs   | shifting |       80 |    2425.99 |     2575.78 |          0 |       1440 |         1198 |            560 |                0 |                    0 |              63.672    |             56.8872     |                      0 |
| B1_g0_3_go_with_compile   | tpchs   | shifting |       80 |    3682.84 |     3828.08 |          3 |        190 |          652 |            546 |             1482 |                 1292 |             148.467    |            133.164      |                      3 |
| C0_g0_3_goc_no_compile    | tpchs   | shifting |       80 |    2419.56 |     2567.61 |          0 |       1440 |         1198 |            560 |                0 |                    0 |              63.2383   |             56.3898     |                      0 |
| C1_g0_3_goc_with_compile  | tpchs   | shifting |       80 |    3696.15 |     3841.5  |          3 |        190 |          652 |            546 |             1482 |                 1292 |             149.364    |            133.981      |                      3 |


### Compile trace summary

| variant                   | bench   | wtype    |   trace_rows |   compile_rejected |   compile_rejected_frac |   kept_added_not_picked |   picked_rows |
|:--------------------------|:--------|:---------|-------------:|-------------------:|------------------------:|------------------------:|--------------:|
| A1_g0_2_base_with_compile | tpchs   | random   |          784 |                416 |                0.530612 |                     187 |           180 |
| A1_g0_2_base_with_compile | tpchs   | noisy    |         1248 |                443 |                0.354968 |                     604 |           201 |
| A1_g0_2_base_with_compile | tpchs   | shifting |         1039 |                710 |                0.683349 |                     162 |           167 |
| B1_g0_3_go_with_compile   | tpchs   | random   |         1386 |                951 |                0.686147 |                     187 |           247 |
| B1_g0_3_go_with_compile   | tpchs   | noisy    |         1824 |                897 |                0.491776 |                     720 |           207 |
| B1_g0_3_go_with_compile   | tpchs   | shifting |         1518 |               1085 |                0.714756 |                     242 |           191 |
| C1_g0_3_goc_with_compile  | tpchs   | random   |         1386 |                951 |                0.686147 |                     187 |           247 |
| C1_g0_3_goc_with_compile  | tpchs   | noisy    |         1824 |                897 |                0.491776 |                     720 |           207 |
| C1_g0_3_goc_with_compile  | tpchs   | shifting |         1518 |               1085 |                0.714756 |                     242 |           191 |


### Merge trace summary

| variant                  |   selected_merge_rows |   selected_group_legacy |   selected_order_legacy |   selected_covering |
|:-------------------------|----------------------:|------------------------:|------------------------:|--------------------:|
| B0_g0_3_go_no_compile    |                   655 |                     299 |                     356 |                   0 |
| B1_g0_3_go_with_compile  |                   411 |                     274 |                     137 |                   0 |
| C0_g0_3_goc_no_compile   |                   655 |                     299 |                     356 |                   0 |
| C1_g0_3_goc_with_compile |                   411 |                     274 |                     137 |                   0 |