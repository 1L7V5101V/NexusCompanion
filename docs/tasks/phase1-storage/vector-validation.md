# Phase 1 向量检索验证：租户过滤 × HNSW 召回

> 日期：2026-08-20
> 目的：回答决策 B 的前置问题——「单全局 HNSW 索引 + `WHERE tenant_id` 过滤」在小租户占比下是否召回退化，以及正确形态是什么。
> 复现脚本与结果在 [tests/](tests/README.md)（本目录随文档入库；本地 PG 17.10 + pgvector 0.8.6）。

## 1. 为什么需要验证

pgvector HNSW 只提供近似最近邻，索引上的 `WHERE tenant_id=?` 是「遍历时逐节点过滤」。若某租户数据在全局向量空间中占比很小（5000 用户系统里单用户通常 ≤1%），HNSW 图遍历主要经过其他租户的节点，被过滤掉后难以在限定的遍历步数内凑齐该租户的 top-k——召回可能坍缩。这个相互作用**必须上线前测**，否则「Phase 1 真正切过去」会变成「部分租户检索劣化」。

## 2. 方法

- **数据**：150,000 行、128 维、2000 个质心聚类 + 高斯噪声（`sigma=0.12`），归一化——模拟真实 embedding 的最近邻结构。第一版基准用均匀随机向量，128 维下全部近似正交、最近邻无意义，全局 recall 仅 0.33，已被废弃。
- **租户分配**：与聚类无关（对抗性场景，租户不可向量分离）。份额 50%→0.05% 严格精确，剩余 11.15% 归 `t_rest` 不测量。
- **Ground truth**：`SET enable_indexscan=off; SET enable_bitmapscan=off` 强制 seq scan 精确 top-10。
- **近似**：默认 planner，`SET hnsw.ef_search` 10/40/100/200，`WHERE tenant_id=? ORDER BY embedding <=> ?::vector LIMIT 10`。
- **每租户 30 个查询向量**（从该租户自身采样，已偏向乐观：真实用户查询不会都落在自家记忆附近）。
- 全局无过滤控制：0.993 @ ef=40，证明索引本身健康。

## 3. 结果 A：单全局 HNSW + 过滤

| 租户 | 占比 | 行数 | plan | ef=10 | ef=40 | ef=100 | ef=200 |
|------|------|------|------|-------|-------|--------|--------|
| t_50pct | 50% | 75000 | index | 0.477 | 0.987 | 0.987 | 0.990 |
| t_20pct | 20% | 30000 | index | 0.257 | 0.800 | 0.947 | 0.963 |
| t_10pct | 10% | 15000 | index | 0.150 | 0.463 | 0.767 | 0.827 |
| t_05pct | 5% | 7500 | index | 0.117 | 0.257 | 0.467 | 0.597 |
| t_02pct | 2% | 3000 | index | 0.110 | 0.180 | 0.277 | 0.413 |
| t_01pct | 1% | 1500 | index | 0.103 | 0.133 | 0.200 | 0.257 |
| t_005pct | 0.5% | 750 | index | 0.087 | 0.123 | 0.137 | 0.193 |
| t_002pct | 0.2% | 300 | index | 0.090 | 0.110 | 0.113 | 0.120 |
| t_001pct | 0.1% | 150 | index | 0.080 | 0.100 | 0.107 | 0.107 |
| t_0005pct | 0.05% | 75 | **seq** | 1.000 | 1.000 | 1.000 | 1.000 |

延迟（ef=40）：索引路径 1.4–2.1ms；t_0005pct 走 seq scan **231ms**（exact_ms 基准 196ms）。

**解读**：
1. 占比 ≤20% 召回就开始明显退化；≤1% 基本只剩 0.1–0.13（ef=40）。ef 提到 200 也仅到 0.257——调参救不回来。
2. planner 对最小租户放弃索引改全表扫描，召回回到 1.000 但延迟 x150。生产 1M+ 行时 seq scan 更慢，planner 会更倾向保留索引——也就是**小租户要么召回坍缩、要么延迟爆炸，二选一**。
3. 查询向量从租户自身采样是乐观偏置，真实场景退化只会更严重。

## 4. 结果 B：对照（同一数据、同一查询、ef=40）

| 租户 | 全局+过滤 | 租户独立索引 |
|------|-----------|--------------|
| t_50pct (75000) | 0.987 | 0.993 |
| t_02pct (3000) | 0.180 | 0.990 |
| t_001pct (150) | 0.100 | 1.000 |
| t_0005pct (75) | 1.000 (seq) | 1.000 |

**结论**：退化是全局图遍历的机制问题（遍历被其他租户节点带偏，过滤饿死目标租户节点），**不是数据稀疏**——同样数据按租户隔离后召回立即回到 0.99+。

## 5. 结果 C：缓解方案生产形态验证

原生分区 + 每分区 HNSW 索引：

```sql
CREATE TABLE items_part (tenant_id text NOT NULL, id bigint NOT NULL,
    embedding vector(128) NOT NULL) PARTITION BY LIST (tenant_id);
CREATE TABLE items_part_<tid> PARTITION OF items_part FOR VALUES IN ('<tid>');
CREATE INDEX ON items_part USING hnsw (embedding vector_cosine_ops);
```

父表建索引 → 各分区自动建 HNSW。查询 `WHERE tenant_id=? ORDER BY embedding<=>?` 分区裁剪到单分区 + 分区内索引扫描（EXPLAIN 确认：`Index Scan using items_part_<tid>_embedding_idx on items_part_<tid>`）。

| 租户 | 行数 | recall@10 (ef=40) | ms/查询 |
|------|------|-------------------|---------|
| t_50pct | 75000 | 0.977 | 1.22 |
| t_02pct | 3000 | 0.987 | 1.22 |
| t_01pct | 1500 | 1.000 | 0.67 |
| t_001pct | 150 | 1.000 | 0.23 |
| t_0005pct | 75 | 1.000 | 0.25 |

全量级召回 0.977–1.000、延迟 0.2–1.2ms，无 seq 回退。

## 6. 结论与影响

- **决策 B 定为**：pgvector + **`PARTITION BY LIST (tenant_id)` + 每分区 HNSW 索引**。废弃「单全局 HNSW + 过滤」。
- 附带收益：分区裁剪加速所有 tenant 作用域查询（非向量检索也受益）、维护/VACUUM 按分区、未来可对热租户单独调参。
- 前提：embedding 从 Text 改原生 `vector(1024)` 列（M2）。

## 7. 局限

- 合成聚类数据（128 维）模拟真实 embedding；真实 1024 维 + 真实分布（更弥散）绝对数值会变，机制与相对量级不变。接入真实数据后需在 M7 用实际 embedding 复测一次。
- 生产需要 `max_rows_per_scan` 心智：HNSW+过滤若未来仍用全局索引，pgvector 有 `hnsw.max_scan_tuples` 之类保护可调，但分区方案下不依赖它。

## 8. 复现

```bash
# 前提：本地 PG 17 + pgvector 0.8.6 已装，vecbench 库存在（见 tests/README.md 或 pg.py）
cd docs/tasks/phase1-storage/tests
PYTHONPATH="D:\.Projects\NexusCompanion\.claude\worktrees\scaling-phase1-storage\tmp\pgvector-setup\py-deps" \
  "D:\.Projects\NexusCompanion\.venv\Scripts\python.exe" recall_bench2.py     # 结果 A + B
PYTHONPATH="D:\.Projects\NexusCompanion\.claude\worktrees\scaling-phase1-storage\tmp\pgvector-setup\py-deps" \
  "D:\.Projects\NexusCompanion\.venv\Scripts\python.exe" partition_test.py   # 结果 C
```
