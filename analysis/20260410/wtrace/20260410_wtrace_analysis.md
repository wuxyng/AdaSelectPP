# 20260410 TRACE=1 分析

## 主结论

- adaptive 与 fixed **已经真实分开运行**：fixed 模式下 TRACE 里的 `lambda` 对所有文件都是常数，且严格等于 case 的 `alpha`；adaptive 模式下 `lambda` 会在几乎所有 round 中动态变化，范围通常覆盖 `0.2` 到 `0.95`。
- `wdcg=1` 下，虽然 adaptive 的 `lambda` 真实在动，但 **最终 `new` 配置与 fixed 几乎完全一致**。除了 `tpch_random` 有 4 个 round 出现单个 key 差异外，其余所有 workload 在 `wdcg=1` 下都是每轮完全一致。
- `wdcg=0` 下，adaptive 和 fixed 的 `new` 配置差异明显得多；这说明 lambda policy 的影响主要还在传统主路径上，而在当前 G0-2 + WDCG 主线里，候选面和排序已经把结果稳定在几乎相同的最终配置。
- 当前 TRACE CSV 记录的是 **实际使用的 `lambda`**，不是 `lambda_shadow`。因此 fixed 模式下你看到的常数 lambda 就是真实生效值；如果后续还要分析 shadow TS/Trigg 行为，需要把 `lambda_shadow` 也落到 CSV。
- 本批 trace/log 里 **没有**发现 `alpha` 与 `fixed_lambda` 不一致的 warning；这一批 fixed/adaptive 对比在参数层面是干净的。

## 关键证据

### 1) fixed 模式的 lambda 是真正常数

- `tpchs_noisy / wdcg1 / fixed`: `lambda_unique=1`，常数值 `0.600`。
- `tpchs_random / wdcg1 / fixed`: `lambda_unique=1`，常数值 `0.350`。
- `tpchs_shifting / wdcg1 / fixed`: `lambda_unique=1`，常数值 `0.850`。
- `tpch_random / wdcg1 / fixed`: `lambda_unique=1`，常数值 `0.500`。

### 2) adaptive 模式的 lambda 是真实动态的，而且是 per-index 动态

- adaptive 模式下，同一轮内部经常出现多个不同的 lambda 值。这说明当前实现的 lambda 不是“每轮一个全局值”，而是 **per-index** 动态值。
- `tpchs_noisy / wdcg1 / adaptive`: `round_mean_avg=0.606`，`lambda_min=0.200`，`lambda_max=0.950`，`rounds_with_lambda_variation=95`。
- `tpchs_random / wdcg1 / adaptive`: `round_mean_avg=0.638`，`lambda_min=0.200`，`lambda_max=0.950`，`rounds_with_lambda_variation=24`。
- `tpchs_shifting / wdcg1 / adaptive`: `round_mean_avg=0.718`，`lambda_min=0.200`，`lambda_max=0.950`，`rounds_with_lambda_variation=79`。
- `tpch_random / wdcg1 / adaptive`: `round_mean_avg=0.647`，`lambda_min=0.200`，`lambda_max=0.950`，`rounds_with_lambda_variation=24`。

### 3) 但 wdcg=1 下 adaptive/fixed 的最终新配置几乎完全一样

- `tpchs_noisy / wdcg1`: `same_new_rounds=96/96`，`avg_new_jaccard=1.000`。
- `tpchs_random / wdcg1`: `same_new_rounds=25/25`，`avg_new_jaccard=1.000`。
- `tpchs_shifting / wdcg1`: `same_new_rounds=80/80`，`avg_new_jaccard=1.000`。
- `tpch_noisy / wdcg1`: `same_new_rounds=96/96`，`avg_new_jaccard=1.000`。
- `tpch_random / wdcg1`: `same_new_rounds=21/25`，`avg_new_jaccard=0.971`。
- `tpch_shifting / wdcg1`: `same_new_rounds=80/80`，`avg_new_jaccard=1.000`。
- 唯一明显例外是 `tpch_random / wdcg1`，有 4 个 round 出现差异；差异键主要是在 `lineitem(l_partkey,l_shipdate)` 和 `lineitem(l_orderkey)` 之间切换。

### 4) wdcg=0 下 adaptive/fixed 差异明显得多

- `tpchs_noisy / wdcg0`: `same_new_rounds=1/96`，`avg_new_jaccard=0.164`。
- `tpchs_random / wdcg0`: `same_new_rounds=5/25`，`avg_new_jaccard=0.507`。
- `tpchs_shifting / wdcg0`: `same_new_rounds=1/80`，`avg_new_jaccard=0.405`。
- `tpch_noisy / wdcg0`: `same_new_rounds=9/96`，`avg_new_jaccard=0.617`。
- `tpch_random / wdcg0`: `same_new_rounds=13/25`，`avg_new_jaccard=0.850`。
- `tpch_shifting / wdcg0`: `same_new_rounds=53/80`，`avg_new_jaccard=0.797`。

### 5) width-2 在 G0-2 主线下保持稳定恢复

- `tpch_random / adaptive / wdcg1`: 平均 `width1=7.28`，`width2=2.72`，`width2占比=27.2%`。
- `tpchs_random / adaptive / wdcg1`: 平均 `width1=8.00`，`width2=2.00`，`width2占比=20.0%`。
- `tpchs_noisy / adaptive / wdcg1`: 平均 `width1=7.00`，`width2=3.00`，`width2占比=30.0%`。
- `tpchs_shifting / adaptive / wdcg1`: 平均 `width1=7.00`，`width2=3.00`，`width2占比=30.0%`。

## 对 fixed-alpha 问题的最终定性

- 当前代码语义下，`fixed_lambda` 已经不是独立实验轴，而是 `alpha` 的兼容别名；`lambda_policy=fixed` 的真实语义是 **constant lambda = alpha_init**。
- 这批 TRACE 已经直接验证了这一点：fixed 模式下的 `lambda` 在所有 case 中都严格恒定，且等于文件名中的 `a...` 值。
- 因此，这一批 run 不存在“你以为在测 fixed，但实际还是按 adaptive/alpha 在跑”的问题。
- 需要保留的结论只是：脚本仍然**无法表达一个独立于 alpha 的 fixed_lambda sweep 轴**；但在这批实验里，它没有把 fixed/adaptive 跑错。

## 当前还缺什么

- 如果你还要继续研究“为什么 adaptive 虽然 lambda 在动，但 `wdcg=1` 下结果几乎和 fixed 一样”，下一步最值的是把 `lambda_shadow` 也打进 TRACE/metrics，并对 `columns_benefit` 的 round-level 演化做 diff。
