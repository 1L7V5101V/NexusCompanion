# M4H-4 5000 tenant 分区 provisioning 基准

> 归属：M4.5 架构硬化 / M4H-4，见 [`m4h-4-partition-provisioning.md`](m4h-4-partition-provisioning.md)
> 分支：`feature/scaling-phase1-storage`
> 运行日期：2026-08-22
> 结果原始数据：[`results/m4h4_partition_provisioning.json`](../../results/m4h4_partition_provisioning.json)

## 1. 为什么要做（依据）

SCALING_PLAN 对 5000 tenant 的 PostgreSQL 分区方案有两处硬性要求：

- **§C 分区生产验证**（[SCALING_PLAN.md](../../scaling/SCALING_PLAN.md) :384-393）：合成 recall 结果不足以证明生产可用，必须验证「5000 分区下的 planning latency、catalog 大小」；「首次写入不在用户 hot path 执行 DDL，改为 tenant provisioning」；「分区名使用稳定 hash/ID，避免简单字符替换导致命名碰撞」。
- **风险表**（:813「5000 LIST 分区运维退化」P1 → 5000 分区基准；:814「首次请求动态 CREATE PARTITION」P1 → provisioning control plane + 幂等 DDL worker）。

M4H-4 的架构决策是「两阶段 readiness gate（`request_provisioning` + `require_ready`）+ 独立 control worker 执行幂等 DDL」——turn 路径绝不执行 DDL。本基准要回答的问题是：

1. **provisioning 延迟**：worker 把一个 tenant 从提交到 READY 的收敛速度（顺序逐个 vs 5000 全量 burst），验证 fail-fast 语义下新 tenant 首个请求要等多久。
2. **catalog 规模**：5000+ 分区后 `pg_inherits` 分区数与 catalog 查询延迟是否仍可控。
3. **planning latency**：5000 分区下目标 tenant 查询的 plan 阶段成本。
4. **分区裁剪**：`WHERE tenant_id` 物理上只扫描目标分区（跨 tenant 不可能碰别的分区）。
5. **失败重试收敛**：advisory 锁竞争下 worker 不会死等无限，失败可重入队收敛。

## 2. 怎么做的（方法与实现）

### 2.1 环境

- 本地 dev PostgreSQL：`postgresql://nexus:nexus_dev@localhost:5433/nexus`（`NEXUS_TEST_PG_URL` 可覆盖）。
- 前置条件：已应用分区迁移 `alembic a3d5c7e9f1b2_partition_memory_items`，父表 `memory_items` 存在（`PARTITION BY LIST (tenant_id)`）。
- 进程：单进程、单 worker task、连接池 `pool_size=20`。

### 2.2 harness

`tests/provision_5000_bench.py` 是独立脚本（pytest 不收集 `provision_*_bench.py`），直接实例化生产代码：`StorageRuntime`（PG backend + `run_db`）→ `TenantProvisioningService` → `TenantProvisioningWorker`，不 mock 存储层。worker 用真实 advisory 锁 + `to_regclass` 双检 + `CREATE ... PARTITION OF ... FOR VALUES IN` + commit。

运行：

```bash
D:\.Projects\NexusCompanion\.venv\Scripts\python.exe tests/provision_5000_bench.py \
  --tenants 5000 --pool-size 20 --poll-interval 0.01 --out results/m4h4_partition_provisioning.json
```

参数：`--tenants`（默认 5000，env `BENCH_TENANTS`）、`--pool-size`（默认 20）、`--poll-interval`（worker 轮询周期，默认 0.01）、`--out`（结果路径，默认 `results/m4h4_partition_provisioning.json`）、`--keep`（保留创建的分区供检查）。默认运行结束后清理全部创建的分区（`DROP TABLE IF EXISTS` 逐名）。

### 2.3 各指标测法

| 指标 | 测法 |
|---|---|
| `sequential` | 逐 tenant `await request_provisioning(tenant)` 后以 0.002s 轮询等状态 READY（30s 超时），记录每 tenant 从提交到 READY 的端到端延迟，汇总 p50/p95/p99/max。 |
| `concurrent_burst` | 5000 tenant 一次性全部入队，worker 并发 drain（advisory 锁使 DDL 全局串行），`_wait_for` 等全部 READY 的总耗时与 per-tenant 均摊。 |
| `catalog` | 基准前记 `pg_inherits` 分区数 baseline，burst 完成后记 after，`delta = after - baseline`；同时记一次 catalog 计数查询延迟。 |
| `planning` | 对采样 tenant 执行 `EXPLAIN (FORMAT JSON)`（`SELECT ... WHERE tenant_id=$1 AND memory_type='note' AND status='active' ORDER BY created_at DESC LIMIT 10`），记 plan 阶段延迟。 |
| `pruning` | 同一 tenant 的 `EXPLAIN (FORMAT JSON)` 收集 Relation Name，断言 scanned_relations 恰为目标分区、`pruned_to_single_partition=true`。 |
| `failure_retry` | 独立 service/worker（`max_attempts=2`、`lock_timeout=100ms`、`backoff=0.02`）：另开连接持 advisory 锁 → request → 两次尝试锁超时 → 状态 FAILED（记 `failed_after_ms`）→ 释放锁 → 重新 request → 等 READY（记 `requeue_converged_ms`）。 |

结果写入 `--out` 指向的 JSON（config + 全部指标），控制台打印摘要。

## 3. 结果

运行配置：`tenants=5000`、`pool_size=20`、`poll_interval=0.01`，完整数据见 [`results/m4h4_partition_provisioning.json`](../../results/m4h4_partition_provisioning.json)。

| 指标 | 结果 |
|---|---|
| sequential 延迟 | total 114.2s / mean 22.9ms / p50 **23.1ms** / p95 **34.0ms** / p99 **47.9ms** / max 82.4ms |
| concurrent burst | 5000 tenant 全量收敛 **18.3s**（per-tenant 均摊 3.67ms） |
| catalog | baseline 189 → after 10189（**delta +10000**），count 查询 15.5ms |
| planning latency | **98.6ms**（单次冷 EXPLAIN） |
| 分区裁剪 | `scanned_relations=[memory_items_bench_burst_4999_43932_d09e24c2b1a5]`、selected_count=1、`pruned_to_single_partition=true` |
| 失败重试 | 锁占用下 224.7ms FAILED（2 attempts）→ 释放后重入队 94.5ms 收敛 READY |

### 3.1 解读

- **顺序 provisioning p50 23.1ms / p99 47.9ms**：这是「提交 job → worker 收敛 READY」的端到端时间（含一个 poll 周期 0.01s 与 DDL）。注意它**不是用户请求路径延迟**——请求在 PENDING 时立即 fail-fast 返回 retryable 错误，绝不等待 provisioning。该数字验证的是 worker 后台收敛速度：即使新 tenant 首请求 miss，一个 poll 周期内分区就绪。
- **burst 5000 tenant 全量 18.3s**：一次性冷启动 5000 个分区。advisory 锁使 DDL 串行，5000 个 CREATE 均摊 3.67ms/个，收敛时间由「串行 DDL 吞吐」主导而非锁死等待。这证明冷启动窗口可接受，且与「进程重启后 `to_regclass` 只读探测恢复 READY」结合，常态下不会每个请求都 miss。
- **catalog delta +10000 / 查询 15.5ms**：10189 个分区下 catalog 计数仍毫秒级。`_assert_partition_ready` 的只读 `to_regclass` 探测在同样规模下是单点 O(1)，无退化。
- **planning 98.6ms 是冷 EXPLAIN 成本**：5000 LIST 分区下 planner 需要评估分区 bound 才能裁剪目标分区，这是单次 plan 阶段开销，不含执行。它说明 plan 成本随分区数增长，是 SCALING_PLAN §C 预留的「若 5000 LIST 分区未通过 gate，再评估 hash bucket」的观测点——M7 需在生产硬件、真实查询形态下复测（含 plancache/预编译语句命中后的热路径）。
- **pruning 单分区**：EXPLAIN 证明 `WHERE tenant_id` 裁剪后只剩目标分区，物理层面杜绝跨 tenant 扫描。这是「跨 tenant 空结果」的底层保证。
- **失败重试收敛**：advisory 锁被占时 worker 在 `lock_timeout`（100ms）内放弃、重试至 max_attempts 后 FAILED（~225ms），不无限阻塞；FAILED 可重新入队并在锁释放后 ~94.5ms 收敛 READY。验证了失败路径不会把 worker 或连接池拖死。

### 3.2 局限

- 本地 dev PG（单机 localhost），绝对延迟不代表生产硬件绝对值，仅作相对验证与方法学基线；M7 需生产基准。
- 单进程单 worker，advisory 锁全局串行 DDL；多进程 `worker_replicas>1` 需要 DB 持久化 provisioning 状态表（ADR §1.5 明确不在 M4H-4 范围）。
- planning 只测单次冷 EXPLAIN，未覆盖热查询/预编译语句路径。
- catalog baseline 189 为 dev 库既有测试残留分区，delta（+10000）才是本基准净增。

## 4. 复现与校验

- 复现：重跑 §2.2 命令即可（会再次创建并清理 10001 个分区）。
- 数据完整性：结果 JSON 是唯一原始数据源；本文档表格只取其中字段，如需核对以 JSON 为准。
- 与 ADR 的一致性：benchmark 是 M4H-4 §2 commit 6 的交付物，metrics 覆盖 ADR §1.3 承诺的 provisioning 延迟、catalog/分区数、planning latency、分区裁剪、失败重试收敛。
