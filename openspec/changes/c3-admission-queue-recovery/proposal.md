# C3 tenant admission + bounded queue + restart recovery

> 对应任务计划：`openspec/openspec-tasks-bundle/task-03-admission-queue-recovery.md`（PILOT_ROADMAP §5.9.10 第 3 项）。
> 输入的已冻结决策（§5.9.5 / §5.9.6 / §6.1 A·B / §10 DECIDED Tenant admission、Queue 容量初始值）不在此重复论证，design.md 逐条引用。
> 跨阶段 change：本 change 覆盖 **P0 段（主体）**；P3 段（恢复演练收尾）依赖 C2 durable 表，验收留待 C2 落地后按 task-03 P3 段单独收口。

## Why

当前单体的执行边界与 Pilot「租户互不影响」目标冲突：

- `agent/control/runtime.py::ConversationRuntime._admission` 是**进程级** `asyncio.Lock`，不同 tenant 的 turn 排进同一条全局串行路径，一个 tenant 的慢 LLM/tool/consolidation 会阻塞所有 tenant；
- `bus/queue.py::MessageBus` 的 inbound/outbound 队列、`bootstrap/passive_worker.py` 的 per-session lane 队列全部**无界**，慢 tenant 可耗尽内存；
- lane key 是 channel-specific `session_key`（`channel:chat_id`），不是 §5.9.5 冻结的 tenant admission key；
- 进程内队列/task 在重启时全部丢失，没有统一启动补偿扫描，`unknown` / `compensation_required` 语义完全没有落地；
- LLM/embedding/MCP/process 四类资源没有分类并发上限，某一类慢资源可耗尽其他资源额度。

C3 是批次 0 并行根（与 C1 并行，E9 弱耦合）；C1 canonical identity 已落地（commit `e124dbf8`），本 change 在切换点对齐 canonical tenant 解析 seam。C4（WebChat dev 闭环扩展）与 P1GATE 都以本 change 的 tenant-scoped admission + 有界队列为前置（E2、D2）。

## What Changes

- **新模块 `agent/admission/`**：
  - `lanes.py` — `TenantLaneRouter`：lane key = 服务端派生 `tenant_id`；同 tenant 串行（interactive/maintenance 各一条执行链）、跨 tenant 异步；interactive 优先于 maintenance；lane owner 在成功/失败/取消/超时/异常路径统一释放；`close()` 后拒绝新 work（进程关闭不产生新 work）。
  - `queues.py` — `AdmissionLimits`（§10 DECIDED 冻结初始值，可配置）+ `BoundedAdmissionQueue` + `AdmissionOverloadError`（携带 `retry_after`，供 HTTP 429+Retry-After / WS overload 事件映射）。
  - `resources.py` — `ResourceSemaphores`：LLM=30 / embedding=4 / MCP=8 / process=2 分类计数、互不挤占。
  - `recovery.py` — 启动恢复扫描框架：`RecoveryAction`（replay/recompute/compensate/cancelled/missed/intentionally_skipped）、`ToolOutcomeStatus`（`unknown` / `compensation_required`）、`RecoveryRecord`（`recovery_started_at`/`recovery_finished_at`/`recovery_action`/`recovery_result`/work id/attempt，供 C12 §7.1 指标复用）、`RecoverySource` 协议 + `StartupRecoveryScanner`。
- **接线**：
  - `ConversationRuntime`：全局 admission lock → per-tenant admission（Pilot 一 tenant 一规范会话，单用户 dev 路径行为不变）；
  - `PassiveMessageWorker`：lane key `session_key` → tenant admission key；per-tenant pending interactive 上限 16，第 17 条明确拒绝；
  - `MessageBus`：global interactive ingress ready queue 有界 128，满载抛 `AdmissionOverloadError`（durable acceptance 前 overload，§5.9.5）；
  - `MarkdownMemoryMaintenance`：per-session pending 意图合并（per-kind ≤1）+ 全局在途 maintenance ≤64，满时延后（不拒绝用户消息）；
  - `MemoryOptimizer`：进程级单 lock → per-tenant lock；
  - `WebChatChannel` outbound：soft 192（丢弃非 durable delta + `replay_required`）/ hard 256 或累计 payload 1 MiB（overload close）；
  - LLM/embedding/MCP/process 四个调用接缝 acquire 对应 semaphore；
  - bootstrap 启动执行 `StartupRecoveryScanner`，扫描非终态 turn 并产出 `RecoveryRecord`。
- **丢失窗口记录**：`agent/admission/LOST_WINDOWS.md` 逐项登记进程内 queue/task 的已知丢失窗口（P0 出口文档断言）。
- **测试**：时间线观测测试（同 tenant 串行/跨 tenant 异步）、overload 测试矩阵、资源 semaphore 上限测试、interactive>maintenance 优先级测试、lane owner 释放测试（成功/失败/取消/超时/异常）、恢复扫描语义断言、丢失窗口文档断言。

## Capabilities

### New Capabilities

- `admission-queue-recovery`：tenant-scoped admission（同 tenant 串行、跨 tenant 异步）、有界队列与 overload 语义（128/16/64/1/256-soft192-1MiB）、分类资源 semaphore（30/4/8/2）、interactive>maintenance 优先级与 maintenance 合并/延后、启动恢复扫描框架与 `unknown`/`compensation_required` 语义、lane owner 统一释放与关闭语义。

### Modified Capabilities

- 无（既有 spec 不修改 requirement；`observability-load` 的指标消费、`canonical-identity` 的 tenant 解析为单向依赖本 change 的产出）。

## Non-Goals（明确不做）

- **不触碰 C2 边界**：不建 inbox_records / turns / tool_calls / background_work_items / outbound_delivery_intents durable 表；恢复扫描的 durable 来源、tool outcome 查询与补偿执行闭环等 C2 落地后接入（本 change 只交付框架与 P0 可用来源）。
- **不做 C5 auth**：admission tenant 解析在 adapter 边界沿用 `tenant_id_for_channel()`（§5.9.5 E9 弱对齐 sanctioned），canonical mapping 切换只改 `agent/admission/lanes.py::resolve_admission_tenant()` 单点。
- **不实现 HTTP 429/WS overload 帧的渠道侧映射**：C3 交付类型化 `AdmissionOverloadError`（含 `retry_after`）与语义契约；渠道 HTTP/WS 响应映射归 C4/C5。
- **不做用户 schedule 恢复**：`missed` 语义仅为枚举预留，`(job_id, scheduled_for)` 幂等恢复归 C11。
- **不做 P3 恢复演练**：每任务一行 replay/recompute/compensate/... 断言报告属 P3 段，等 C2 durable 表。
- **不改变 consolidation 业务语义**：`memory_window=40`/`keep_count=20`/threshold=30/失败阻断 turn 全部保持（§10 DECIDED）。

## Impact

- **代码**：新增 `agent/admission/`（5 个文件）+ `agent/admission/LOST_WINDOWS.md`；修改 `agent/control/runtime.py`、`bootstrap/passive_worker.py`、`bus/queue.py`、`core/memory/markdown.py`、`proactive_v2/memory_optimizer.py`、`infra/channels/web_chat_channel.py`、`agent/provider.py`、`memory2/embedder.py`、`agent/mcp/client.py`、`agent/tools/shell.py`、`bootstrap/app.py`（接线）。
- **配置**：新增 `[agent.admission]` 可选配置段（容量初始值 = §10 DECIDED 冻结值，未配置即默认；压测后由后续 change 调整）。
- **依赖**：无新增第三方依赖。
- **测试**：新增 `tests/admission/`（无 PG 依赖，CI 可跑）；既有 turn/runtime/passive/webchat/memory 测试回归。
- **行为变化**：多 tenant 并发 turn 从全局串行变为跨 tenant 异步（Pilot 目标行为）；单用户 dev（单 tenant）执行顺序不变。重启后非终态 turn 将被扫描并标记 `cancelled`（此前为悬挂 `queued/in_progress` 记录）。
