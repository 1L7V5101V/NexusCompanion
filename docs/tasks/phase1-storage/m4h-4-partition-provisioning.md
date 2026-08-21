# M4H-4 分区 provisioning（计划 + ADR）

> 状态：已完成（2026-08-22，8 commits `4c50a0e0`–`f056d5ea`）
> 归属：M4.5 架构硬化，见 [`m4.5-architecture-hardening.md`](m4.5-architecture-hardening.md)
> 分支：`feature/scaling-phase1-storage`
> 依据：[`SCALING_PLAN.md`](../../scaling/SCALING_PLAN.md) §C 分区生产验证（:384-393）与风险表 :814

## 1. ADR：partition provisioning 控制面

**决策：两阶段 readiness gate（`request_provisioning` + `require_ready`）+ 独立 control worker 执行幂等 DDL。**
turn 路径**绝不**执行 `CREATE PARTITION`；store 写路径不再隐式建分区；真正 DDL 由独立 worker 经 `StorageRuntime.run_db`（pool 连接 + 独立事务）执行。

### 1.1 背景与问题（M4H-4 盘点结论）

- 唯一分区 DDL 在 `PostgresMemoryStore._ensure_partition()`（[postgres_memory_store.py:278-307](../infra/storage/postgres_memory_store.py)），被 3 个写路径调用：`upsert_item`:334、`upsert_consolidation_event`:409、`merge_item_raw`:518。读路径从不触发（父表查询对未建分区 tenant 返回空，跨 tenant 空结果天然成立）。
- 首次写入的 upsert 事务内执行 `CREATE PARTITION` → 用户 hot path 上的不可控 DDL 延迟，SCALING_PLAN §C:391 明令「首次写入不在用户 hot path 执行 DDL」。
- `_sanitize_tenant`（:95）是有损字符替换（`telegram:123` 与 `telegram_123` → 同名分区）→ 命名碰撞风险，§C:392 要求「分区名使用稳定 hash/ID」。
- 单一全局 advisory key（`_PARTITION_LOCK_KEY = 872_001_457`，:53）序列化所有 tenant 的分区创建 → 5000 tenant 并发 provisioning 的收敛点，需 `lock_timeout` 限界等待。
- `_check_pg_schema`（factory）是建池前的单次 pre-flight schema 探测，**不是** tenant provisioning（M4H-3 偏离记录已述，本任务不触碰）。
- sessions/messages/turns 未分区，不受影响。

### 1.2 决策与取舍

| 方案 | 取舍 | 结论 |
|---|---|---|
| **turn 内同步 provisioning**（before_turn 模块直接 `await provision_tenant`） | DDL 仍在首次请求内执行，只是把位置从写事务挪到 turn 开始；「动态 DDL 仍属于用户 hot path」 | 不选（用户裁定） |
| **两阶段 gate + 独立 control worker** | `request_provisioning` 幂等提交 job、`require_ready` 只读检查；真正 DDL 由独立 worker 经 `run_db` + pool + 独立事务执行。首次请求 PENDING → fail-fast（`retryable=True`），worker 后台完成，后续请求 READY 直接通过。DDL 延迟从请求路径彻底消除。 | **选定** |
| 纯前置批量 provisioning | 新 tenant 有机上线必须依赖外部预建，流程断裂 | 不选 |

### 1.3 架构

```
turn 路径（用户请求）                              control 路径
ConversationRuntime._run                         TenantProvisioningService
  admission（agent/control/runtime.py:175）          ├─ worker task（async）
    └─ TurnStartup gate（admission 之后、              │    └─ await run_db(provision_tenant_sync)
         TurnExecutor:216 之前）                      │         ├─ pool 借出连接
         ├─ request_provisioning(tenant)             │         ├─ pg_advisory_xact_lock（+lock_timeout）
         │     UNKNOWN: 只读 to_regclass 探测        │         ├─ to_regclass 双检
         │       存在 → READY（不建 job）              │         ├─ CREATE ... PARTITION OF ... FOR VALUES IN
         │       缺失 → PENDING + 入队                │         └─ commit → READY（缓存 commit 后更新）
         └─ require_ready(tenant)                    └─ 失败 → rollback → 重试(backoff) → FAILED
               READY   → TurnExecutor 继续
               PENDING → 抛 PartitionNotReady（retryable）
               FAILED  → 抛 PartitionProvisioningFailed
```

**状态机**：`UNKNOWN → PENDING → READY`；`PENDING → FAILED`（尝试耗尽）；`FAILED` 可经 `request_provisioning` 重新入队。

- **`request_provisioning` 幂等**：同 tenant 只在 UNKNOWN→PENDING 时入队一次；PENDING/READY/FAILED 重复调用不重复入队。UNKNOWN 时先做只读 `to_regclass` 探测——进程重启后已存在的分区立即恢复 READY（避免每次重启后所有 tenant 首个请求误 fail-fast），不执行任何 DDL。
- **`provision_tenant`（同步执行器）幂等**：advisory 锁 + `to_regclass` 双检，分区已存在则跳过 CREATE 直接 READY；事务失败由 M4H-3 `connection()` 包装 rollback，无 aborted transaction / 半初始化态；缓存只在 commit 后更新。
- **并发**：同 tenant 并发 → 全局 advisory 锁串行化，恰好一次有效 DDL，其余等待后复用结果；异 tenant 并发 → 串行但各自完成。`lock_timeout`/`statement_timeout` 限界等待，provisioning 延迟可测（基准）。per-tenant 锁留 M7 基准驱动，不在本任务引入。
- **触发点（生产链路）**：TurnStartup 放 `ConversationRuntime._run` admission 之后、TurnExecutor 之前，接收服务端已解析的 tenant（经 `TurnRequest.metadata["tenantId"]`，接线时补 [`passive_worker.py:85-99`](../bootstrap/passive_worker.py) 的 metadata）。proactive 目标 tenant 执行前 `require_ready`，PENDING 跳过本轮。dashboard/undo 等直写入口只允许 READY tenant，不在请求内 provisioning。
- **store 写路径 fail-fast**：删除 `_ensure_partition`，改 `_assert_partition_ready`——冷缓存时只做只读 `to_regclass` 探测，缺分区抛 `PartitionNotReady`，**绝不执行 DDL**。

### 1.4 稳定分区命名规则

`memory_items_{sanitize(tenant)[:30]}_{md5(tenant)[:12]}`：

- 可读前缀（`_sanitize_tenant` 有损替换仅用于显示）+ **48-bit md5 后缀保证唯一**（5000 tenant 碰撞概率 ~4e-8）。
- 总长 ≤ 56 ≤ PG 63 字符标识符限制；分区 bound 值仍是原始 tenant_id（`sql.Literal`，含 `:` 等字符）。
- 现有 dev 分区名随之变化：预 merge 阶段无生产数据，dev 重建即可，不做向后兼容查找。

### 1.5 明确的边界（不做）

- 不把存储 Protocol 改 async（Phase 2「单进程并发与阻塞隔离」）。
- 不做 DB 持久化 provisioning 状态表（多进程 `worker_replicas>1` 时才需要，M7）。
- 不按 tenant 建 advisory 锁、不做请求内等待 provisioning 的 queue（PENDING 采用 fail-fast + `retryable`）。
- 不触碰 sessions/turns（未分区）、`_check_pg_schema`、markdown 旧记忆系统。

## 2. 提交序列（7 commits）

1. **provisioning 状态模型、接口、ADR**（本 commit）：本文档 + `infra/storage/provisioning.py`（`PartitionStatus` / `TenantProvisioning` seam / 哨兵异常）+ `m4.5` §M4H-4 补计划链接 + `tests/test_provisioning_seam.py` 最小契约测试。
2. **稳定 partition name/hash 规则**：`partition_name_for_tenant` + 碰撞/长度/注入测试。
3. **幂等 provisioning 实现（seam + 状态机 + 同步执行器）**：状态注册表 + `request_provisioning`（含只读探测恢复）+ `provision_tenant` 同步执行器（advisory 锁 + 双检 + CREATE + commit）+ 同/异 tenant 并发与事务恢复测试。
4. **独立 control worker**：async worker task + 队列 + 并发去重 + retry/backoff + rollback + 启停 + 测试。
5. **接入 TurnStartup/Proactive 并移除写路径懒 DDL**：`ConversationRuntime._run` gate + `passive_worker` metadata 带 tenant + proactive `require_ready`/跳过 + dashboard/undo READY-only + store `_ensure_partition` → `_assert_partition_ready`。
6. **5000 tenant 基准 harness**：`tests/provision_5000_bench.py` + `results/`（provisioning 延迟、catalog/分区数、planning latency、分区裁剪、失败重试收敛）。
7. **文档证据与 M4H-4 checkbox**：`m4.5` / `phase1-storage.md` 勾选 + 基准结果入库。

## 3. 验证命令

每 commit 跑对应定向 pytest + pyright；main 基线保持 37，Phase 1 storage 范围 0 新增。相关定向集：

```bash
D:\.Projects\NexusCompanion\.venv\Scripts\python.exe -m pytest -q -W error tests/test_provisioning_seam.py tests/test_storage_factory.py tests/test_storage_parity.py tests/test_storage_runtime.py tests/test_tenancy.py tests/test_tenant_isolation.py tests/test_storage_pool.py
npx --no-install pyright --venvpath D:\.Projects\NexusCompanion --level error
npx --no-install pyright --venvpath D:\.Projects\NexusCompanion --project pyrightconfig.tests.json --level error
```

## 4. 实现证据与偏离记录

**状态：已完成（2026-08-22，8 commits，`4c50a0e0`–`f056d5ea`）**

按 §2 序列实现：

1. ADR + seam/state 模型：`4c50a0e0`
2. 稳定分区命名：`ec7d445a`（`partition_name_for_tenant`：可读前缀 + 48-bit md5 后缀，bound 值保留原始 tenant_id）
3. 幂等 provisioning 服务：`fa31b228`（状态机 + `request_provisioning` 只读探测恢复 + 同步 DDL 执行器）
4. 独立 control worker：`2ca420be`（同 tenant 去重、异 tenant 并行、retry/backoff、失败 FAILED）
5. 接入 + 移除懒 DDL：`1dd09385`（store 写路径 fail-fast）、`9ac95d83`（TurnStartup/Proactive/接线）、`8736740c`（dashboard/undo READY-only 验证）
6. 5000 tenant 基准：`f056d5ea`

**基准结果**（`results/m4h4_partition_provisioning.json`，本地 dev PG，5000 tenant，pool_size 20，poll_interval 0.01）：

| 指标 | 结果 |
|---|---|
| sequential 延迟 | total 114.2s / mean 22.9ms / p50 23.1ms / p95 34.0ms / p99 47.9ms |
| concurrent burst | 5000 tenant 全量收敛 18.3s（3.67ms per tenant） |
| catalog | baseline 189 → after 10189（delta +10000），lookup 15.5ms |
| planning latency | 98.6ms（EXPLAIN FORMAT JSON） |
| 分区裁剪 | scanned_relations=1、pruned_to_single_partition=true |
| 失败重试 | 锁占用 224.7ms FAILED（2 attempts）→ 释放重入队 94.5ms 收敛 READY |

**偏离记录**：

- §2 计划 7 commits，commit 5 实现时拆为 5a/5b/5c 三个可验证 concern（写路径 fail-fast / turn gate + proactive + bootstrap wiring / dashboard-undo READY-only 验证），实际 8 commits。
- 基准 catalog 记 delta 而非绝对数：dev 库 baseline 189 为既有测试残留分区，清理不属于本任务数据，如实记录增量 +10000（seq 5000 + burst 5000）。
- 其余与 §1.3 定案一致：turn 路径零 DDL（store 写路径只读 probe）、proactive 执行前 `require_ready` 且 PENDING/FAILED 跳过本轮、dashboard/undo 直写缺分区 0 行 no-op 且不触发 provisioning。
