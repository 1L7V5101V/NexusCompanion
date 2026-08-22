# M4H-3 connection 与 event-loop 隔离（计划 + ADR）

> 状态：ADR 已定案（sync pool + bounded executor），实现已完成（8 commits，2026-08-21）
> 归属：M4.5 架构硬化，见 [`m4.5-architecture-hardening.md`](m4.5-architecture-hardening.md)
> 分支：`feature/scaling-phase1-storage`

## 1. ADR：连接模型选型

**决策：sync pool（`psycopg_pool.ConnectionPool`）+ 有界 executor（`concurrent.futures.ThreadPoolExecutor`）。**

### 1.1 背景与问题

M4H-2 之后的状态：`PostgresMemoryBackend` / `PostgresSessionBackend` 各持有**一条** psycopg 连接 + `threading.RLock`；`TenantStorage` view 不拥有连接。`StorageConfig.pool_size`（=20）当前**不被 sync adapter 消费**。所有 PG 方法在调用方线程同步执行；业务调用方（turn lifecycle phases、SessionManager、memory engine/memorizer/retriever、presence worker、dashboard handlers、undo）运行在 asyncio 内，直接同步调 DB 会阻塞 event loop。并发开启前必须修复，否则只是把 SQLite 全局锁换成单连接锁（SCALING_PLAN 风险行）。

### 1.2 备选与取舍

| 方案 | 取舍 | 结论 |
|---|---|---|
| **async adapter**（psycopg async / SQLAlchemy async） | 需把 Storage Protocol（同步方法）、两个 adapter 40+ 方法、全部调用方与测试改写为 async。M4H-2 刚收口 interface 与 tenant 贯穿；在其上改 async 属 Phase 2 范畴，风险远超本目标。SCALING_PLAN 将其列为长期推荐目标，不在此处做。 | 不选 |
| **sync pool + bounded executor** | 保留同步 Protocol 与调用方签名；`pool_size` 真正驱动连接预算；asyncio 边界用 `run_in_executor` 隔离同步 DB 调用；同步入口（CLI、非 asyncio 后台线程）直接同步调，两条路径共用同一 pool，天然由 pool 互斥串行。改动面收敛在基础设施 + 调用边界。 | **选定** |
| 手写连接池（`queue.Queue`） | 需自实现借出超时、坏连接清理、`max_waiting`、shutdown 语义；psycopg3 官方 `psycopg_pool.ConnectionPool` 已提供（含 `configure` 回调与 `check()`），无理由重复造轮子。 | 不选 |

选择理由（同用户指示）：
- M4H-2 已完成 `TenantStorage` request-scoped seam，view 不拥有连接——为「pool 是唯一连接所有者」铺平。
- 现有 PG adapter 方法基本都是同步接口；async 化会同时改写大量调用方、Protocol 和测试。
- sync pool + bounded executor 先保证 DB 调用离开 event loop，同时保留现有 storage interface。

### 1.3 资源所有权模型

```
进程级（bootstrap 一次）                    请求级（每 turn/request）
StorageRuntime                              TenantStorage（view）
  ├─ PgConnectionPool（sync ConnectionPool）   ├─ TenantContext
  │    min_size / max_size = pool_size         ├─ 轻量 tenant-bound view
  │    configure: register_vector + dict_row   └─ 不创建 / 不拥有 / 不关闭连接
  ├─ ThreadPoolExecutor（bounded）
  └─ close(): executor.shutdown(wait) → pool.close()
```

- **连接从 pool 借出、用完归还**；事务失败先 rollback 再归还；连接状态由 `pool.check()` 清理（借出时校验，坏连接替换）。
- **`pool.max = StorageConfig.pool_size`**（DB 连接预算，配置真实生效）。
- **executor 线程预算**：SCALING_PLAN §597-601 预算公式 `sum(worker_replicas × worker_pool_max) + gateway_pool`；当前生产单进程（`worker_replicas=1`），故 executor 线程数取 `pool_size`，DB 调用并发与连接数对齐，避免线程无界堆积在 pool 等待。
- **shutdown 顺序**：先 `executor.shutdown(wait=True)`（等运行中的 DB 调用完成，不再接受新提交）→ 再 `pool.close()`。不依赖 `__del__`（M4H-2 backend 的 `__del__`/close 语义保留给 owned 测试路径）。
- **禁止**（继承 M4H-2 + 本 ADR）：每 tenant 建长期 PG store；每请求创建/关闭 PG connection；`TenantStorage.close()` 关闭进程级 pool；event loop 中直接调用同步 psycopg；`PostgresMemoryStore`/`PostgresSessionStore` constructor 继续充当资源所有者（owned 构造只保留给测试与显式 single-store 调用方）；用增大 `pool_size` 掩盖 executor 或数据库预算问题。

### 1.4 阻塞隔离机制

- **view 方法保持同步**（Storage Protocol 不变，`MemoryStorage`/`SessionStorage` 签名零改动）。
- **asyncio 调用方**在进入 DB 的边界用 `await loop.run_in_executor(executor, fn)`（或经 runtime 提供的 submit 辅助）；一段只做同步 DB 调用的操作块整段提交，不在方法间反复切线程。
- **同步入口**（CLI、非 asyncio 后台线程）直接同步调 view，不经过 executor。
- **event-loop lag probe**（commit 7）用可重复 probe 证明隔离生效：asyncio 侧跑一个 DB 调用时 event loop 仍能按预期周期响应。

### 1.5 依赖

- 新增 **`psycopg-pool>=3.2`**（psycopg3 官方 pool 扩展）到 `requirements.txt` + `pyproject.toml`。
- pool 连接 `configure`：memory 侧 `register_vector(conn)`（pgvector 需**每条连接**注册）；session 侧 `row_factory=dict_row`（与现状 `PostgresSessionBackend` 一致）。`ConnectionPool(..., configure=...)` 每次借出连接时调用，正好覆盖。

### 1.6 明确的边界（不做）

- 不把 Storage Protocol 改 async（Phase 2「单进程并发与阻塞隔离」）。
- 不实现 PG turns 表（`ConversationRuntime` 保持 SQLite-only）。
- 不碰 markdown 旧记忆系统、`ProactiveDashboardReader` 的 proactive 日志库、`runtime_race_probe`。
- 不动 WIP async engine（`bootstrap/db/engine.py`，`DatabaseConfig.pool_size`，当前无生产引用）。

## 2. 前置调研记录（2026-08-21）

1. **`pool_size` 定义处**：`agent/config_models.py:95` `StorageConfig.pool_size=20`（生产路径唯一来源）；`bootstrap/db/config.py:11` `DatabaseConfig.pool_size=20`（WIP async engine 专用，无生产引用）；`agent/config.py:299` 从 toml `[storage].pool_size` 解析。sync adapter 当前不消费。
2. **建连接的 PG constructor**：`PostgresMemoryBackend.__init__`（postgres_memory_store.py:153，`psycopg.connect` + `register_vector`）；`PostgresSessionBackend.__init__`（postgres_session_store.py:59-71，`connect` + `row_factory=dict_row`）；owned 构造 `PostgresMemoryStore(url,...)`/`PostgresSessionStore(url,...)` 触发 backend 建连接；`factory.py:_check_pg_schema` 每调用临时 `psycopg.connect`（schema 探测）；`bootstrap/dashboard_api.py:858` 自建 `StorageRuntime`。
3. **直接同步调 PG 的路径**：所有经 `runtime.for_tenant(ctx).memory/sessions` 的 view 方法（`self._conn.execute` 在调用方线程同步执行）；调用方 = turn lifecycle phases、SessionManager、memory engine/memorizer/retriever、presence worker、dashboard handlers、undo。
4. **bootstrap shutdown 顺序**：`bootstrap/tools.py` `build_core_runtime` 建 `storage_runtime`；`CoreRuntime.close()` 内 :329 `session_manager.close()` → :375 `"storage_runtime.close"`（`_close_storage_runtime`）→ spawn/mcp/event_bus；`bootstrap/app.py:642 shutdown()` 先走 servers → control → conversation_runtime → memory_runtime.aclose → http_resources，最后 `core.close()`。dashboard 自建 runtime（`dashboard_api.py:933` close）。
5. **连接预算**：SCALING_PLAN §597-601 `sum(worker_replicas × worker_pool_max) + gateway_pool`；§685 `worker_count = ceil(target_active_turns / measured_safe_turns_per_worker) × headroom`。当前单进程 `worker_replicas=1` → `pool_size` 即 DB 连接预算。

## 3. 提交序列（8 commits）

1. **ADR/设计决策**（`34c0f4c8`）：`m4h-3-connection-isolation.md` + `m4.5-architecture-hardening.md` §M4H-3 补计划链接。
2. **StorageRuntime 资源所有权与显式 shutdown**（`8c9e96fe`）：runtime 持 pool + executor；`close()` 顺序 executor → pool；view 不持有 pool/executor；sqlite 路径不受影响。
3. **PG pool adapter**（`55787a9e`）：`psycopg_pool.ConnectionPool` 封装（min/max=pool_size、timeout、max_waiting、`configure=register_vector+dict_row`）；`pool_size` 真实生效；owned 构造保留测试用。
4. **同步 DB 调用移出 event loop**（`3094b880`）：asyncio 边界 `run_in_executor`（turn memory/session 调用、dashboard handlers、undo）；同步入口直接调。
5. **transaction rollback/recovery**（`c61d4d75`）：借出连接失败先 rollback 再归还；`pool.check()` 清理坏连接；aborted transaction 恢复测试。
6. **pool exhaustion 与并发测试**（`1f85359a`）：并发借出、借出超时、`max_waiting` 上限、连接归还后复用。
7. **event-loop lag probe**（`8eddd528`）：可重复 probe 证明 DB 调用不阻塞 event loop。
8. **文档证据与 Phase 1 checkbox**（本 commit）：m4h-3 证据、`m4.5` §M4H-3 checkbox `[x]`、`phase1-storage.md` 勾 gate。

## 4. 验证命令

每 commit 跑对应定向 pytest + pyright；main 基线保持 37，Phase 1 storage 范围 0 新增。相关定向集：

```bash
D:\.Projects\NexusCompanion\.venv\Scripts\python.exe -m pytest -q -W error tests/test_storage_factory.py tests/test_storage_parity.py tests/test_storage_interfaces.py tests/test_storage_runtime.py tests/test_tenancy.py tests/test_tenant_isolation.py tests/test_storage_pool.py
npx --no-install pyright --venvpath D:\.Projects\NexusCompanion --level error
npx --no-install pyright --venvpath D:\.Projects\NexusCompanion --project pyrightconfig.tests.json --level error
```

## 5. 实现证据与偏离记录（2026-08-21）

M4H-3 全部 8 commits 已完成并提交（哈希见 §3）。实现证据已汇总到 [`m4.5-architecture-hardening.md`](m4.5-architecture-hardening.md) §M4H-3：pool 预算真实生效、executor 线程数=pool_size、shutdown 顺序 executor→pool、借出异常先 rollback、`pool.check()` 清理坏连接、并发/耗尽/超时/`max_waiting` 测试、可重复 event-loop lag probe（对照组直接同步调用漏拍 ~200ms，`run_db` 隔离下心跳保持 ~10ms）。

**偏离记录**：ADR §3.3 原约定「`_check_pg_schema` 改经 pool 临时借出」，实现保留临时 `psycopg.connect`。原因：`_check_pg_schema` 是建池前的单次 pre-flight schema 探测（`create_store`/`create_session_store`/`create_storage_runtime` 入口，pool 尚不存在），为单次探测临时建池会白白拉起 min_size 连接；偏离仅限一次性探测，真实借出路径全部经 pool。§3.3 已按实现更新。
