# M4H-2 tenant 运行时接线（计划）

> 状态：已完成（A–F 全部提交，2026-08-21）
> 归属：M4.5 架构硬化，见 [`m4.5-architecture-hardening.md`](m4.5-architecture-hardening.md)
> 分支：`feature/scaling-phase1-storage`
> 证据：A `4ac8d8c8` · B `8bffd85b` · C `597feda3` · D1–D3 `4b27de4b`/`e464e6bb`/`abce3d45` · E `555d7079` · F-1 `c7b5daee`（既有测试同步）· F `cda8af11`（test_tenant_isolation.py + 文档）

## Context

M4H-0/M4H-1 已完成并提交（`f4697e27`）。M4H-1 定义了 `MemoryStorage`/`SessionStorage`
Protocol 与 `TenantContext`/`TenantResolver` seam，但 tenant 尚未真正贯穿运行时：
`create_store`/`create_session_store` 与两个 PG adapter 仍默认 `tenant_id="default"`，
生产调用点没有从可信 inbound identity 派生 tenant。当前 parity 证明的是 adapter 行为，
不是跨 tenant 隔离。

M4H-2 的目标：把 tenant 从**可信 inbound identity** 解析出来，贯穿
session/memory/dashboard/proactive/undo/source_ref/附件，业务调用点通过
`storage_runtime.for_tenant(context)` 获取 tenant-bound view，移除多用户路径隐式
`"default"`。物理连接与 event-loop 隔离（pool/executor）留到 M4H-3。

## 已锁定的决策（2026-08-21）

1. **M4H-2 范围 = 逻辑 tenant 接线**；M4H-3 = 物理连接与 event-loop 隔离。
2. **禁止方案**：每 tenant 缓存 Postgres\*Store 实例；每请求创建并关闭一个 PG store；
   给 40+ 存储业务方法都暴露 tenant_id 参数；用 ContextVar 隐式读 tenant；从客户端
   metadata/query parameter/session_key 反推可信 tenant。
3. **Seam 定案**：`StorageRuntime`（进程级、bootstrap 创建一次）→ `for_tenant(TenantContext)`
   → `TenantStorage`（turn/request-scoped 轻量 view，含 `memory`/`sessions`）。view 只绑定
   TenantContext、不拥有连接、不暴露 close；`for_tenant()` 必须廉价（不建
   connection/pool/per-tenant 单例）；只有 `StorageRuntime.close()` 关底层。
4. **Tenant 映射 = 一律多用户派生**：Telegram/QQ/CLI 一律由 channel 身份派生 tenant
   （`f"{channel}:{chat_id}"`）；`"default"` 只允许出现在测试与显式 single-user 入口。
5. **Memory 深度 = 完整贯穿**：每次 memory 操作经 `runtime.for_tenant(显式 tenant).memory`
   解析；接受 engine/memorizer/retriever + 调用链签名的改动面。
6. **提交拆分 A-F**（下节）。

## 目标架构

### StorageRuntime / TenantStorage seam

```
进程级（bootstrap 一次）                      每 turn/request
StorageRuntime ──for_tenant(ctx)──► TenantStorage(ctx)
  │ memory_backend（PG conn+RLock）            ├── memory: MemoryStorage   (tenant-bound view)
  │ session_backend（PG conn+RLock）           └── sessions: SessionStorage (tenant-bound view)
  └─ sqlite: MemoryStore2 + SessionStore（共享，忽略 tenant）
```

### PG adapter 的 resource-owner / tenant-bound view 拆分

- 新增 `PostgresMemoryBackend` / `PostgresSessionBackend`：持有连接 + `threading.RLock`
  + `_closed`；memory 侧额外持有 `register_vector` 结果与 `_partitions_known`（分区存在性是
  catalog 级，天然跨 tenant 共享）。
- store 保留「owned 构造」（`PostgresMemoryStore(url, tenant_id, vec_dim)`）供 factory/tests
  直连；新增 view 构造（`PostgresMemoryStore(backend, tenant_id)`，`_owns_backend=False`）。
- 关键技巧：`self._conn`/`self._lock` 改为委托 backend 的 property，方法体零改动
  （memory 66+28 处、session 60+29 处引用原样可用）；`_partitions_known` 移到 backend；
  `_check_open`/`close`/`__del__` 走 backend，view 的 `close()` 是 no-op 且不标记 backend 关闭。
- SQL 仍显式使用 view 的 `tenant_id`（现状已是 `WHERE tenant_id = %s`，不动）。

### Tenant 派生与 threading 规则

- `tenant_id = f"{channel}:{chat_id}"`，由可信 inbound identity（适配器从 Telegram/QQ 服务器
  收到的 chat_id、CLI 输入、程序化调用方）在服务端派生，永不接受客户端传的 tenant。
- tenant 在**入站边界解析一次**，经 `InboundMessage.tenant_id` 显式携带贯穿；不在存储调用点反推。
- 全程**显式参数传递**，禁止 ContextVar。存储 Protocol 方法不新增 tenant 参数（view 已绑定）；
  Memorizer/Retriever/engine/worker 等业务封装方法新增 `tenant` 参数（用户接受的「调用链签名」改动）。
- **fail-closed**：忘记解析 tenant 就触碰存储的路径，要么编译期缺失（`InboundMessage.tenant_id`
  必填），要么显式抛错（`infra/storage/tenancy.py:assert_tenant_resolved`），绝不静默落到 `"default"`。

## 提交序列

### A. TenantContext 字段与各 channel TenantResolver 策略

- `infra/storage/tenancy.py`（新）：`tenant_id_for_channel` / `resolve_tenant` /
  `assert_tenant_resolved` / `DEFAULT_TENANT`。
- `m4.5-architecture-hardening.md` §3.1 补 per-channel tenant 策略表（Telegram/QQ/CLI/
  programmatic/WebChat/Feishu；群聊 = 单一 tenant）。
- `tests/test_tenancy.py`（新）：映射确定性、跨 tenant 不同、空值 fail-closed。

### B. turn/inbound/event/proactive 上下文传播

- **B1 inbound + 适配器**：`InboundMessage.tenant_id` 必填；Telegram（消息分发、`/stop`、
  `_on_response`）、QQ（含 group 直连 `session_manager.get_or_create` 绕过 bus 的路径）、CLI
  在构造点调 `resolve_tenant`。
- **B2 turn 上下文**：`process_direct_message(tenant_id=...)` / `_process` 放入 `TurnState`；
  `TurnState.tenant_id` property = `msg.tenant_id`；`TurnRequest.metadata["tenantId"]`。
- **B3 事件**：`TurnCommitted`/`TurnIngested` 加 `tenant_id`；`after_turn.py` 填充；engine 转发。
- **B4 proactive**：`build_proactive_runtime` 从 default_channel/default_chat_id 解析 tenant。

### C. StorageRuntime/TenantStorage seam 和 bootstrap 生命周期

- 两个 PG store backend/view 拆分（构造签名向后兼容）。
- `infra/storage/runtime.py`（新）：`StorageRuntime` / `TenantStorage`。
- `infra/storage/factory.py`：新增 `create_storage_runtime`；`create_store`/`create_session_store`
  保留（tests + sqlite + 显式 single-store 调用方）。
- `bootstrap/tools.py:build_core_runtime`：建 runtime 一次，分发给 SessionManager/engine/
  presence/dashboard/proactive。
- `tests/test_storage_runtime.py`（新）。

### D. SessionManager、memory 等持有者改为 tenant-bound 使用

- `Session` 加 `tenant_id`；`get_or_create(tenant_id, key)`；cache/lock 键改 `(tenant_id, key)`；
  `_store` 改经 runtime 每操作解析。
- engine/memorizer/retriever：`store_for: Callable[[TenantContext], MemoryStorage]`，方法加
  `tenant` 参数；`MemoryQuery`/`MemoryIngestRequest` 加必填 `tenant`。
- presence、meta tools 同规则；7 个 `get_or_create` 调用文件全带 tenant。

### E. dashboard/undo/source_ref/附件隔离

- dashboard 自建 StorageRuntime，读接口按 tenant_id scope，去/显式 gate 跨 tenant 全量列表。
- undo 从触发消息 tenant_id 解析 view；source_ref/附件确认 PG 已按 tenant_id scope。

### F. A/B tenant isolation 测试与文档证据

- `tests/test_tenant_isolation.py`（新，`@pytest.mark.postgres`）：memory CRUD/search、
  session list、dashboard、undo/source_ref 的 A/B 越权。
- `m4.5-architecture-hardening.md` M4H-2 五个 checkbox `[x]` + 证据；`phase1-storage.md` 勾 gate。

## 已知约束（M4H-2 不解除，但必须显式处理）

- **Turn 持久化 SQLite-only**：`ConversationRuntime` 需 `create_turn/transition_turn/read_turn`，
  `SessionManager.control_store` 对 PG 抛 `RuntimeError`。M4H-2 不实现 PG turns 表。
- **SQLite = 显式 single-user**：sqlite store 无 tenant 维度，runtime 的 sqlite 路径忽略
  tenant_id、返回共享 store；测试并文档化「sqlite 不提供跨 tenant 隔离」。
- **WebChat/Feishu 未实现**：只写策略，代码不存在即 fail-closed（无入口可越权）。
- **markdown 旧记忆系统不动**：已按 channel/chat_id 文件路径天然隔离，`storage.backend` 不影响。

## 不改动的确认项

- 不实现 PG turns 表、pool/executor（M4H-3）、markdown 旧记忆系统、`ProactiveDashboardReader`
  的 proactive 日志库、`runtime_race_probe`。
- 不给存储 Protocol 40+ 方法加 tenant 参数（view 已绑定）。
- 不删除 `create_store`/`create_session_store`；`create_storage_runtime` 是新增生产入口。

## 验证

每 commit 跑对应定向 pytest + pyright；main 基线保持 37，Phase 1 storage 范围 0 新增。

全量门禁（2026-08-21，F 后）：`pytest -q -W error --continue-on-collection-errors -k "not test_serve_smoke_loads_config_and_runs_shutdown"` 相对 main 基线
main 83 failed/806 passed/5 收集错误 → 分支 84 failed/884 passed/0 收集错误；分支独有失败仅 `test_kernel_phase_order.py` 2 个（断言未实现的 `ProactivePhaseRunner` stub 行为，生产与 main 字节一致，main 因 conftest 收集错误从未运行，属 main 潜在缺陷非本分支引入）。

```bash
D:\.Projects\NexusCompanion\.venv\Scripts\python.exe -m pytest -q -W error tests/test_storage_factory.py tests/test_storage_parity.py tests/test_storage_interfaces.py tests/test_storage_runtime.py tests/test_tenancy.py tests/test_tenant_isolation.py
npx --no-install pyright --venvpath D:\.Projects\NexusCompanion --level error
npx --no-install pyright --venvpath D:\.Projects\NexusCompanion --project pyrightconfig.tests.json --level error
```
