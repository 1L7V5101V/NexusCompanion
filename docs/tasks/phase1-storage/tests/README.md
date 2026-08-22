# Phase 1 向量检索验证测试（租户过滤 × HNSW 召回）

> 决策 B 的前置验证。为什么测、测什么、结果如何、怎么复现，全部在这里。
> 结论文档见 [vector-validation.md](../vector-validation.md)，本目录是可运行的脚本 + 已归档结果。

## 1. 为什么做这个测试

pgvector 的 HNSW 是**近似**最近邻搜索。生产查询形如
`WHERE tenant_id=? ORDER BY embedding <=> ?::vector LIMIT ?`，租户过滤是遍历时逐节点做。
当某租户的数据在全局向量空间中占比很小（5000 用户系统里单用户通常 ≤1%），HNSW 图遍历主要经过其他租户的节点，
目标租户的节点被过滤掉后难以在限定的遍历步数内凑齐——**召回可能坍缩**。

如果不提前测，"Phase 1 真正切过去"就会变成"切过去但部分租户检索劣化"。所以上线前必须量化
「租户占比 × HNSW 召回」的相互作用，并确定正确的索引形态。

## 2. 需要的东西（环境）

- 本地 **PostgreSQL 17** + **pgvector 0.8.6**（Windows 原生安装见下）
- Python 3 + `psycopg2-binary`（本机装在 `tmp/pgvector-setup/py-deps/`，脚本用 `PYTHONPATH` 指向它）
- `vecbench` 数据库（`postgresql://postgres@localhost:5432/vecbench`），需 `CREATE EXTENSION vector`

### pgvector Windows 原生安装（Docker 不可用时的替代）

1. 从 `andreiramani/pgvector_pgsql_windows` 的 release 下载对应 PG 大版本的 `vector.*-pg17.zip`
2. `lib/vector.dll` → `pgsql/lib/`，`share/extension/vector*` → `pgsql/share/extension/`
3. `CREATE EXTENSION vector;` 单独执行（与 `SELECT vector_version()` 合在一起会因后者不存在而整体回滚）
4. 注意：此构建无 `gen_random_vector()`，造数据用 `(SELECT array_agg(random())::vector FROM generate_series(1,128))`

## 3. 两个脚本

| 脚本 | 回答的问题 | 产物 |
|------|-----------|------|
| `recall_bench2.py` | 单全局 HNSW + 租户过滤，召回是否随占比崩？planner 是否回退 seq scan？租户独立索引能否恢复？ | 结果 A + B（`results/recall_bench2.txt`） |
| `partition_test.py` | 缓解方案的生产形态（原生 LIST 分区 + 每分区 HNSW）是否达标？ | 结果 C（`results/partition_test.txt`） |

`recall_bench2.py` 可选参数：`--total`（默认 150000）、`--g` 质心数（2000）、`--nq` 每租户查询数（30）、
`--dim`（128）、`--sigma` 噪声（0.12）、`--efs`（10,40,100,200）、`--skip-load`（复用已有 items 表重测查询）。

## 4. 复现

```bash
# 依赖：若 tmp/pgvector-setup/py-deps 不存在，先装 psycopg2
python -m pip install --target tmp/pgvector-setup/py-deps psycopg2-binary

cd docs/tasks/phase1-storage/tests
PYTHONPATH="D:\.Projects\NexusCompanion\.claude\worktrees\scaling-phase1-storage\tmp\pgvector-setup\py-deps" \
  "D:\.Projects\NexusCompanion\.venv\Scripts\python.exe" recall_bench2.py 2>&1 | tee results/recall_bench2.txt
PYTHONPATH="D:\.Projects\NexusCompanion\.claude\worktrees\scaling-phase1-storage\tmp\pgvector-setup\py-deps" \
  "D:\.Projects\NexusCompanion\.venv\Scripts\python.exe" partition_test.py 2>&1 | tee results/partition_test.txt
```

`partition_test.py` 依赖 `recall_bench2.py` 先跑过（从 `items` 表建分区）。全量跑约 3–4 分钟（加载 18s + 全局索引 83s + 查询/对照）。

## 5. 数据与测量要点

- **数据**：150k 行、128 维、2000 质心聚类 + 高斯噪声（sigma=0.12）归一化——模拟真实 embedding 的最近邻结构。
  第一版用均匀随机向量，128 维下全部近似正交、无最近邻结构，全局 recall 仅 0.33，数据废弃（见 vector-validation.md §2）。
- **租户分配**：与聚类无关（对抗性场景），份额 50%→0.05% 严格精确，剩余 11.15% 归 `t_rest` 不测量。
- **Ground truth**：`SET enable_indexscan=off; SET enable_bitmapscan=off` 强制 seq scan 精确 top-10。
- **plan 标注**：`EXPLAIN` 判断走 index 还是 seq，区分"索引召回坍缩"和"planner 回退全表"两种机制。
- 别忘了 `ANALYZE`（第一版缺它导致 planner 选不出索引，结果被污染）。

## 6. 关键结论（详细见 vector-validation.md）

1. 全局 HNSW + 过滤：占比 ≤10% 召回崩到 0.46 以下，≤1% 仅 0.10–0.13（ef=40）；ef 拉高到 200 也只到 0.257。
2. 最小租户（0.05%）planner 放弃索引改 seq scan——召回 1.0 但延迟 231ms vs 1.5ms。
3. 对照：同一数据建租户独立索引，召回回 0.99–1.00 → 退化是遍历机制问题，不是数据稀疏。
4. 缓解（生产形态验证）：`PARTITION BY LIST (tenant_id)` + 父表 HNSW（分区自动建索引），召回 0.977–1.000、延迟 0.2–1.2ms。
