# AdaSelect++ Phase 0.5 配置键清单（可维护版）

> 目的：明确哪些 JSON knobs **真的接线生效**，哪些目前只是 **兼容/占位**，避免实验时“以为调了其实没用”。

---

## A. AdaSelect（核心）

### A1. 选择与稳定性（生效）

- `max_num`：每轮最多保留/返回的索引数（生效）
- `min_width`, `max_width`：索引列宽上下限（生效）
- `beta`：切换阈值（生效）
- `optimizer_ratio` / `ratio`：what-if 预算比例（生效）
- `timeout`：超时阈值（生效）

> **Dead-zone 统一修复**：AdaSelect 在 `_choose_config` 中已与 LiteSelect 的 dead-zone 逻辑对齐：当 `|old_benefit|` 近 0 时不再触发“无限提升”切换，避免 oscillation。

### A2. Lambda（EMA/自适应）（生效）

- `lambda_policy`：`adaptive` / `fixed`（生效）
- `fixed_lambda`：固定 lambda 值（仅在 `fixed` 时使用，生效）

### A3. 未评估索引的记忆衰减（生效）

- `benefit_decay`：未评估索引的衰减因子（`None` 表示跟随每条索引自己的 lambda；生效）
- `benefit_decay_fixed`：当 `lambda_policy=fixed` 且 `benefit_decay` 为空时的默认衰减（生效）

---

## B. Phase 0.5 WDCG Candidate Funnel（CandidateGenerator）

### B1. 总开关与基础参数

- `wdcg_enabled`：是否启用 WDCG funnel（生效）
- `wdcg_use_plan`：是否使用 plan extraction（生效）
- `wdcg_topk`：WDCG 选出的 Top-K 规模（生效）
- `wdcg_tau`：候选枚举的 co-occurrence 阈值/温度参数（生效，取决于 enumerator 实现）

### B2. 表大小过滤（**已接线生效**）

- `wdcg_enable_table_filter`：是否启用表统计/过滤模块（生效）
- `wdcg_min_table_rows`：小表硬过滤阈值（生效）
- `filter_min_rows`：`wdcg_min_table_rows` 的**兼容别名**（生效）
- `wdcg_table_stats_batch_size`：一次 SQL 统计查询中表名数组的 chunk 大小（生效）

> 实现方式：用 `pg_stat_user_tables.n_live_tup` 优先，缺失/为 0 时回退到 `pg_class.reltuples`，并将 `base_rows` 回填到 QueryInfo；在生成 roles/tbl_cols 后、进入 rank/select 前完成小表 prune。

### B3. DML-Awareness（**软过滤 + 降权**，已接线生效）

- `wdcg_dml_threshold`：DML churn 阈值（用于触发降权，而不是硬禁用）（生效）
- `wdcg_dml_ema_alpha`：DML EMA 平滑系数（生效）
- `wdcg_dml_ema_tau_sec`：可选：按时间常数自动推导 EMA（>0 生效；=0 使用 alpha）
- `wdcg_dml_soft_enable`：是否启用 DML 软降权（生效）
- `wdcg_dml_soft_k`：降权强度（指数衰减系数）（生效）
- `wdcg_dml_soft_min`：最低保留权重（避免极端误杀）（生效）

> 当前定义的 churn：`writes/(writes+reads)`，其中 writes=ins+upd+del，reads=seq_tup_read+idx_tup_fetch（来自 pg_stat_user_tables）。

### B4. 仍为兼容/占位（当前不生效或弱生效）

以下键目前**保留参数位**，但在当前 CandidateGenerator 实现中不直接影响排序/选择（用于保持 API 兼容，后续可接线）：

- `wdcg_rho`：当前 ranker 中未使用（占位）
- `wdcg_cap_family`：当前未用于 selector 的 family cap（占位）
- `wdcg_coverage_boost`：当前为兼容位（占位）
- `wdcg_cols_per_table`, `wdcg_m_single`, `wdcg_warmup_rounds`, `wdcg_coverage_target`：其中部分对枚举/选择有效，具体要看 enumerator/selector 的实现细节（建议以日志 `wdcg_stats` 验证）。

---

## C. LiteSelect（baseline）相关（生效）

- `transition_mode`：`absolute` / `relative` / `symmetric`（生效）
- `beta`：切换阈值（生效）
- `optimizer_ratio` / `ratio`，`timeout`，`min_width/max_width`（生效）

---

## D. 实验建议：如何验证“接线生效”

1. 开启 `--debug`：看 `WDCG.gen` 输出的 `pruned_small_tables` 与 `dml_tables_downweighted` 是否非 0。
2. 对 JOB：设置 `filter_min_rows=10000`，观察候选数 `candidate_count_raw` 与 `suspicious_aff_count` 是否显著下降。
3. 若 `wdcg_enable_table_filter=false`：上述两个指标应回到旧行为（作为反证）。
