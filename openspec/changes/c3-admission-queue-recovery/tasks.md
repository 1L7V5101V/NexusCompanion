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
- [ ] 5.2 `pytest -q -W error tests/` 全量回归无新增失败。验证：`openspec/evidence/c3-admission-queue-recovery/pytest-regression.txt`
- [x] 5.3 task-03 状态更新（P0 段完成 → P3 段保持 planned 待 C2）；evidence 落 `openspec/evidence/c3-admission-queue-recovery/`
