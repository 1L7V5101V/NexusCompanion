# C3 admission + queue + recovery — 实施任务

> 对应 `openspec/openspec-tasks-bundle/task-03-admission-queue-recovery.md`；P0 段本 change 交付，P3 段（恢复演练）依赖 C2 另行收口。
> 分支：`feature/c3-admission-queue-recovery`（worktree `D:/1/wt-nexus-c3`）。

## 1. admission 模块与 lane

- [x] 1.1 `agent/admission/lanes.py`：`WorkKind`（interactive/maintenance）、`TenantLaneRouter`（同 tenant 串行、跨 tenant 异步、interactive 优先、owner try/finally 统一释放、`close()` 拒绝新 work）、`resolve_admission_tenant()` 单点。验证：`tests/admission/test_lanes.py` 时间线断言
- [x] 1.2 `agent/admission/queues.py`：`AdmissionLimits` 冻结默认值（128/16/64/1、WS 256/192/1MiB）+ `BoundedAdmissionQueue` + `AdmissionOverloadError(limit_kind, retry_after)`。验证：`tests/admission/test_overload.py` 矩阵
- [x] 1.3 `ConversationRuntime` per-tenant admission（tenantId key，空回退 thread_id）。验证：`tests/admission/test_runtime_admission.py` 跨 tenant 异步 + 既有 control 测试回归

## 2. 接线（bus / passive worker / maintenance）

- [x] 2.1 `MessageBus.publish_inbound` 有界 128，满抛 `AdmissionOverloadError`；`bootstrap/passive_worker.py` lane key → tenant、per-tenant pending 16、第 17 条出站拒绝。验证：overload 矩阵测试 + 既有 passive/bus 测试回归
- [x] 2.2 `MarkdownMemoryMaintenance` per-(session,kind) 单意图合并 + 全局在途 maintenance ≤64 延后。验证：`tests/admission/test_priority.py`（interactive 优先 + maintenance 合并/延后/可重算）
- [x] 2.3 `MemoryOptimizer` 进程级单 lock → per-tenant lock；grep 断言无全局 maintenance lock。验证：`tests/admission/test_optimizer_lock.py` 两 tenant 并行 optimize
- [x] 2.4 `WebChatChannel` outbound soft 192（丢 delta + `replay_required`）/ hard 256 或 1MiB（overload close）；final/terminal 不丢。验证：`tests/admission/test_ws_outbound.py` + `tests/test_web_chat_channel.py` 回归

## 3. 资源 semaphore

- [x] 3.1 `agent/admission/resources.py`：`ResourceSemaphores`（LLM 30/embedding 4/MCP 8/process 2，可配置、None 直通）。验证：`tests/admission/test_resources.py` 分类计数互不挤占
- [x] 3.2 四接缝包裹：`LLMProvider.chat`、`Embedder.embed/embed_batch`、`McpClient.call`、`ShellTool.execute`。验证：3.1 测试 + provider/shell/embedder 既有测试回归

## 4. 启动恢复扫描框架

- [x] 4.1 `agent/admission/recovery.py`：`RecoveryAction`、`ToolOutcomeStatus`（unknown/compensation_required）、`RecoveryRecord`（recovery_started_at/finished_at/action/result/work_id/attempt）、`RecoverySource` 协议、`StartupRecoveryScanner`。验证：`tests/admission/test_recovery.py` 语义断言
- [x] 4.2 P0 来源：control store 非终态 turn → `cancelled`；bootstrap 启动接线（`bootstrap/app.py::start()`）+ 结构化日志。验证：test_recovery 集成用例
- [x] 4.3 `agent/admission/LOST_WINDOWS.md` 丢失窗口记录（MessageBus inbound/outbound、passive lane、maintenance 意图、WS outbound、运行中 task）。验证：`tests/admission/test_lost_windows_doc.py` 文档断言

## 5. 回归与状态

- [x] 5.1 `pyright --level error`（project + tests 两配置）无新增错误（36/30 与 main 基线一致）。验证：`openspec/evidence/c3-admission-queue-recovery/pyright-project.txt` + `pyright-tests.txt`
- [x] 5.2 `pytest -q -W error tests/` 全量回归无新增失败。验证：`openspec/evidence/c3-admission-queue-recovery/pytest-regression.txt` + `pytest-regression-env.md`（2026-09-20 于 main @ `5e959e1` 复跑：1193 passed / 166 skipped / 1 failed；唯一失败 `test_plugin_doctor` 为 symlink 权限环境性、非 C3 归因；166 跳过全为 PG 不可用。判读见环境说明）
- [x] 5.3 task-03 状态更新（P0 段完成 → P3 段保持 planned 待 C2）；evidence 落 `openspec/evidence/c3-admission-queue-recovery/`

## 6. 已知缺口与未接线项（P0 段，2026-09-06 复核）

> 本节记录 P0 段**有意未接线**与**已修复缺陷**，供 C8/C9、C12 与后续 reviewer 参考；不代表 P0 验收条件。

- **`TenantLaneRouter` 未接线到生产调度**：生产路径分别使用 `ConversationRuntime._admissions`（per-tenant lock，task 1.3）、`PassiveMessageWorker._lane_queues`（per-tenant 有界队列，task 2.1）、`MarkdownMemoryMaintenance._maintenance_queues`（per-session 单槽，task 2.2）与 `MemoryOptimizer`（per-tenant lock，task 2.3）；router 的 `run_interactive`/`run_maintenance`/`close()` 目前仅由 `tests/admission/test_lanes.py` 覆盖。统一迁入 router 留待 C8/C9 接 `TenantRuntimePlan`（见 design ADR-5 与 proposal Non-Goals）。
- **`BoundedAdmissionQueue` / `AdmissionLimits` 属契约层交付物**：生产有界由 `asyncio.Queue(maxsize=...)` 直接实现（`bus/queue.py::MessageBus._inbound`、`bootstrap/passive_worker.py`），二者作为 C4/C12 接缝的稳定类型导出，当前由 `tests/admission/test_overload.py` 覆盖。
- **已修复**：`TenantLaneRouter.run_interactive` 在「等待 `lane.lock` 期间被取消」时未归还 `interactive_pending`（会让 `wait_interactive_idle` 永久 busy，同 tenant maintenance 永远延后）；改为在 `finally` 中补齐并补测试 `tests/admission/test_lanes.py::test_cancelled_while_waiting_for_lane_lock_releases_pending`。
- **已修复**：`[agent.admission].global_maintenance_queue` 此前被 `agent/config.py` 读取并校验但未被消费（`MarkdownMemoryMaintenance` 用模块常量），现已由 `bootstrap/memory.py` 注入；补测试 `tests/admission/test_priority.py::test_global_maintenance_limit_is_injectable`。
- **P3 段恢复演练**保持 planned（依赖 C2 durable 表）；`unknown` / `compensation_required` 由 recovery 框架冻结语义，P0 来源不产出（design ADR-7）。
- **基线环境依赖未清（2026-09-20 复核）**：main @ `5e959e1` 全量回归 1193 passed / 166 skipped / 1 failed。166 跳过全部为本地 PG 不可用（117 项 `postgres` marker + 测试内 PG 守卫 + `test_fast_rebuild_parity` rachael host 依赖），导致 C1/C2 identity/control-plane 集成断言未被本次运行覆盖；唯一失败 `tests/test_plugin_doctor.py::test_plugin_doctor_reports_healthy_skill_plugin` 为 symlink 权限环境性（`WinError 1314`），该测试缺 symlink 可用性守卫。两项均非 C3 归因，但「全量绿」需先清此两项（补守卫 / 在带 pgvector PG 的环境复跑）。详见 [pytest-regression-env.md](../../evidence/c3-admission-queue-recovery/pytest-regression-env.md)。
