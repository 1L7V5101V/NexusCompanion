# C3 admission + queue + recovery — 设计

> 引用的冻结决策：§5.9.5（容量表/overload 行为）、§5.9.6（恢复语义）、§6.1 A（tenant-scoped serial lane）、§6.1 B（durable recovery/replay）、§10 DECIDED（Tenant admission、Queue 容量初始值、工具取消）。
> 代码锚点均为 2026-09-06 main（`632d3006`）实际行为，行号以当时为准。

## 1. 现状与差距

| 现状（代码事实） | 与 §5.9.5/§6.1A 的差距 |
| --- | --- |
| `agent/control/runtime.py:79` `self._admission = asyncio.Lock()`，`_run()` 全程持锁（`async with self._admission`） | 进程级串行：不同 tenant 的 turn 互相等待，违反「租户互不影响」 |
| `bus/queue.py:126-127` `MessageBus._inbound`/`_outbound` 无界 `asyncio.Queue` | 无 128 上界，无 overload 语义 |
| `bootstrap/passive_worker.py:25` `self._lane_queues: dict[str, asyncio.Queue]` 无界、key=`session_key` | 无 per-tenant 16 上界；lane key 非 tenant |
| `core/memory/markdown.py:1028` per-session `deque` 无界；每个 turn 追加一个意图 token | 无 per-kind 1 合并、无全局 64 上界 |
| `proactive_v2/memory_optimizer.py:216` `self._lock = asyncio.Lock()` 进程级单实例 | optimizer 运行即锁住所有 tenant |
| `infra/channels/web_chat_channel.py:56` `_OUTBOUND_QUEUE_SIZE=256`，满时 `try_enqueue` 返回 False 丢帧 | 无 soft 192 delta 降级、无 1 MiB payload 会计 |
| 四类资源（`agent/provider.py::LLMProvider.chat`、`memory2/embedder.py::Embedder`、`agent/mcp/client.py::McpClient`、`agent/tools/shell.py`）无分类并发上限 | 慢资源可耗尽其他资源额度 |
| 重启后 `turn_audit.db` 中 `queued/in_progress` turn 悬挂，无启动扫描 | 违反 §5.9.6「启动时扫描非终态 turn/tool/work」 |

## 2. ADR

### ADR-1 新模块 `agent/admission/` 承载 C3 全部组件

**结论**：lane router、有界队列、overload、semaphore、恢复扫描集中在新模块 `agent/admission/`，C3 拥有的组件在验收边界内一处可查；`bus/`、`agent/control/`、`bootstrap/` 只做接线。

**备选**：分散进 `bus/queue.py` 与 `agent/control/`。不选：admission 横跨 bus 与 control 两层，分散会让「overload 测试矩阵」与「lane owner 释放」验收跨目录拼接；C4/C12 的接缝（admission seam、观测字段）也需要单一 import 面。

### ADR-2 lane key = tenant_id，解析单点 `resolve_admission_tenant()`

**结论**：admission key 一律 `tenant_id`。`InboundMessage.tenant_id` 由可信 adapter 经 `infra.storage.tenancy.resolve_tenant()` 填充（现状事实，`bus/events.py:31-34` 注释即此契约）；`resolve_admission_tenant(item)` 对空 tenant 在 worker 边界用 `tenant_id_for_channel(channel, chat_id)` 派生（E9 弱对齐 sanctioned），**禁止**落到 `DEFAULT_TENANT`。C1 canonical mapping 切换时只改这一个函数。

**备选**：直接接 `CanonicalIdentityResolver`。不选：channel 层认证归 C5，当前 adapter 无可信 principal，resolver 查表只会把 C1 的 fail-closed 错误提前抛到无认证路径；seam 单点已保证切换成本。

### ADR-3 ConversationRuntime admission 全局锁 → per-tenant 锁

**结论**：`self._admission: asyncio.Lock` 改为 `self._admissions: dict[str, asyncio.Lock]`，key = `request.metadata["tenantId"]`，缺失/空时回退 `thread_id`（单用户 dev 路径单 key，执行顺序与现状一致）。同 thread 的互斥仍由 `_active_by_thread` owner 检查保证（不变），admission 锁只负责「同 tenant 串行、跨 tenant 异步」。

**备选**：去掉 admission 锁只靠 `_active_by_thread`。不选：thread 级 owner 不约束 tenant 级的 Proactive/maintenance 交错，per-tenant 锁是 §6.1A 的直接表达。

**风险**：既有测试若断言「不同 thread 串行」将失败——这是被 §3.3 明确要求的行为变化（「不同 tenant 的执行时间线无交叉等待」），按新契约修测试。

### ADR-4 有界队列用类型化异常表达 overload，不做内部静默丢弃

**结论**：`BoundedAdmissionQueue.try_put_nowait()` 满时抛 `AdmissionOverloadError(limit_kind, retry_after)`。`MessageBus.publish_inbound` 有界 128（满→异常，Telegram 等可重试 channel 不 ack 让 provider 重试，durable acceptance 前的 overload 发生点符合 §5.9.5）；`PassiveMessageWorker` per-tenant 16（满→直接出站明确拒绝消息，不无限堆积）；maintenance 64 满→延后（返回 False，调用方不重试用户消息）。

**备选**：满载阻塞等待。不选：§5.9.5 明确「不得无限阻塞 channel update handler」。

### ADR-5 maintenance 合并语义 = per-(tenant,kind) 单 pending 意图 + 全局 64 在途上限

**结论**：`MarkdownMemoryMaintenance._enqueue_maintenance()` 的 per-session deque 改为单意图槽（已有 pending 时不再追加——consolidation/refresh 本来就从 durable state 重算，重复意图无信息量）；worker 启动前检查全局在途 maintenance 任务数 ≥64 则延后（意图保留，下轮 turn 提交再触发）。interactive lane 永不与 maintenance 共用容量。

**备选**：把 maintenance work 全部迁入 `TenantLaneRouter`。不选：P0 段按 §6.1A 落地顺序只要求「admission key + 有界 queue + overload + 观测」；全量迁移留待 C8/C9 接 TenantRuntimePlan 时一并做，本 change 不重写 consolidation 调度。

### ADR-6 资源 semaphore 在四个调用接缝包裹，进程级共享实例

**结论**：`ResourceSemaphores` 持四类 `asyncio.Semaphore`（LLM 30 / embedding 4 / MCP 8 / process 2，可配置），由 bootstrap 装配注入；四个接缝：

- `agent/provider.py::LLMProvider.chat`（含 streaming 全程持有）；
- `memory2/embedder.py::Embedder.embed/embed_batch`；
- `agent/mcp/client.py::McpClient` 工具调用方法；
- `agent/tools/shell.py::ShellTool.execute`。

semaphore 为 `None` 时零开销直通（测试可注入小容量验证互不挤占）。

**备选**：在 admission lane 层统一 acquire。不选：lane 是 tenant 排他概念，semaphore 是全局资源池概念，混在 lane 里会让「第 31 个 LLM 等待」误伤 tenant 串行语义；§5.9.5 原文也是「admission/queue 之外的全局资源上限」。

### ADR-7 启动恢复扫描框架先行，durable 来源随 C2 接入

**结论**：`agent/admission/recovery.py` 定义 `RecoveryAction` / `ToolOutcomeStatus`（`unknown`/`compensation_required`）/ `RecoveryRecord`（含 `recovery_started_at`/`recovery_finished_at`/`recovery_action`/`recovery_result`/`work_id`/`attempt`）/ `RecoverySource` 协议 / `StartupRecoveryScanner`。P0 内置来源：control store 非终态 turn → `cancelled`（重启即运行中 task 被中断，§6.1B）。`unknown`/`compensation_required` 的 outcome 查询与补偿执行闭环依赖 C2 `tool_calls` 表，本 change 只冻结状态机语义与记录结构。

**备选**：等 C2 一起做。不选：task-03 把「启动恢复扫描框架」列为 P0 产出且是独立并行根；框架先行让 C2 的表直接对着稳定契约设计。

### ADR-8 WS outbound soft/hard 双阈值

**结论**：`WebChatChannel._Connection` 增加累计 payload 字节会计；出队深度 ≥192 时丢弃非 durable `message.delta` 并发 `replay_required`（客户端按 `last_sequence` 补拉）；≥256 或累计 ≥1 MiB → hard overload，以协议 close code 断开。canonical final/terminal 帧永不被丢弃（现状 `_ReplayBuffer` 只缓存 replayable 帧的语义保持）。

### ADR-9 进程关闭不产生新 work

**结论**：`TenantLaneRouter.close()` 与 `ConversationRuntime.shutdown()` 后所有入队入口（`publish_inbound`、worker `_enqueue`、maintenance enqueue）拒绝新 work；既有 `PassiveMessageWorker.run()` finally 取消 lane task、`ConversationRuntime.shutdown()` 取消 turn task 的行为保持，lane owner 释放统一由 router 的 try/finally 保证。

## 3. 配置

```toml
[agent.admission]
global_interactive_queue = 128      # §10 DECIDED
per_tenant_pending_interactive = 16
global_maintenance_queue = 64
llm_concurrency = 30
embedding_concurrency = 4
mcp_concurrency = 8
process_concurrency = 2
ws_outbound_soft_limit = 192
ws_outbound_hard_limit = 256
ws_outbound_max_payload_bytes = 1048576
```

全部可省略；缺省即冻结初始值。压测记录（拒绝率/backlog/429/内存）后才允许后续 change 调整（§10 DECIDED）。

## 4. Risks / Trade-offs / 回滚

| 风险 | 处置 |
| --- | --- |
| 既有测试断言跨 thread 全局串行 | 属 §3.3 要求的行为变化；逐个改为「同 tenant 串行」契约，回归全量 pytest 验证 |
| Shell process=2 可能约束既有并发测试 | 测试经 fixture 注入更小/更大容量或 `None`；生产默认值不变 |
| per-tenant 锁字典随 tenant 数增长 | Pilot 10–30 tenant 量级，dict 开销可忽略；锁随 runtime 生命周期存在，不额外清理（避免 use-after-free 竞态） |
| 恢复扫描误标 | P0 来源只有 turn audit（无外部副作用确认手段），统一 `cancelled` + 文档说明；`unknown`/`compensation_required` 在 C2 表落地前不产出 |
| 回滚 | 整个 change 无 schema/协议变更；回滚 = revert 分支。[agent.admission] 未配置时全部走默认值，单用户行为与 main 等价 |

## 5. 验收映射（P0 段）

| task-03 验收标准 | 证据 |
| --- | --- |
| 不同 tenant 异步、同 tenant 串行 | `tests/admission/test_lanes.py` 时间线观测 + `tests/admission/test_runtime_admission.py` |
| 队列满载行为 128/16/64/1/256-192-1MiB | `tests/admission/test_overload.py` 矩阵 + `tests/test_web_chat_channel.py` 回归 |
| semaphore 30/4/8/2 分类不互耗 | `tests/admission/test_resources.py` |
| interactive 优先、maintenance 可合并/重算 | `tests/admission/test_priority.py` + maintenance 合并测试 |
| 无跨 tenant global maintenance lock、optimizer lock 按 tenant | grep 断言测试 + `tests/admission/test_optimizer_lock.py` |
| lane owner 统一释放、关闭不产生新 work | `tests/admission/test_lane_owner.py` |
| 丢失窗口有明确记录 | `agent/admission/LOST_WINDOWS.md` + `tests/admission/test_lost_windows_doc.py` |
| 恢复扫描逐条可解释 | `tests/admission/test_recovery.py`（P0 来源=非终态 turn → cancelled；P3 段演练随 C2） |
