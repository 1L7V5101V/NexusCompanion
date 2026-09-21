# C15 durable work queue 消费层 — 实施任务

> 对应 `openspec/changes/2026-09-20-c15-work-queue-consumer/`；design.md ADR-1..ADR-9 为实现约束，偏离需先改 design。
> 分支：`feature/c15-work-queue-consumer`（worktree 建议 `D:/1/wt-nexus-c15`，与 C3 同惯例）。
> 证据统一落 `openspec/evidence/c15-work-queue-consumer/`。
> 阶段：P0（durable 消费能力 + 运行期接线）。P3 的恢复演练报告另归 `PILOT_ROADMAP_PROJECT_CHECKLIST` P3 条目，本 change 只提供其所需记录点（§6）。

## 1. DB 迁移与 ORM model（ADR-2）

- [x] 1.1 新增 alembic revision（`down_revision = f3c8a9d2e7b4`）：`background_work_items` 增 `attempt_count INTEGER NOT NULL DEFAULT 0`、`lease_owner VARCHAR(128) NULL`、`lease_expires_at TIMESTAMPTZ NULL`、`next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()`、`last_error TEXT NULL`、**`flow VARCHAR(32) NULL`**（业务链路，决定 handler）；建 `ix_background_work_items_claim (status, next_attempt_at)`；**建只追加审计表 `work_attempts`**（`work_item_id` FK / `outcome` CHECK ∈{succeeded,failed,released,recovered,redrive} / `error` / `started_at` / `finished_at` + `ix_work_attempts_item`）。验证：`alembic upgrade head` + `downgrade -1` + `upgrade head` 可逆（已在真 PG18 验证双向可逆，含新增列与表），见 [`claim-sql-smoke.md`](../../evidence/c15-work-queue-consumer/claim-sql-smoke.md)；`tests/migration/` 回归待 task 7.2（需 pgvector）
- [x] 1.2 `bootstrap/db/models/control_plane.py` 同步：`BackgroundWorkItemModel` 加上述 6 列 + claim 索引；新增 `WorkAttemptModel`；状态 CHECK **不变**（ADR-1）。验证：model `__table__.columns`/`indexes` 断言 + pyright 0 errors + `test_control_plane_contract` 9 passed
- [ ] 1.3 expand-only 兼容性验证：在**旧列**上插入的行（不指定新列）取 DEFAULT/NULL 且可读。验证：新增用例 `tests/control_plane/test_work_queue_migration.py::test_expand_only_backward_compatible`

## 2. `WorkItemRepository`（ADR-1 / ADR-3 / ADR-4）

- [ ] 2.1 `claim_batch(owner, *, batch_size, lease_ttl_seconds)`：单语句原子认领（`FOR UPDATE SKIP LOCKED`，`status→in_progress`，写租约，`attempt_count + 1`）。**due 判据 = `queued` 且到期 OR stale `in_progress`**（(B) 定案：**不含 `failed`、不含 `attempt_count` 门槛** ⇒ 死信结构性不被认领、崩溃不计入预算）。含**两个 per-tenant 条件**：在途互斥 + 轮内去重（ADR-4，形状已在真 PG 验证）。验证：`tests/control_plane/test_work_queue_state_machine.py` 全路径 + 并发认领只成功一次 + `test_claim_skips_tenant_with_active_lease` + `test_failed_terminal_not_claimed`
- [ ] 2.2 `heartbeat(tenant_id, work_item_id, owner, *, lease_ttl_seconds) -> bool`：续租，返回 False = 失租。验证：续租成功/失租两向用例；lease 到期时间与 claim 同源（DB 时钟）
- [ ] 2.3 `record_work_succeeded(tenant_id, work_item_id, owner, *, mutate)`：租约 CAS 前置下推进 `succeeded` + `finished_at` + 清租约，并与 `mutate(session)` 的副作用写入**同事务**（ADR-6）。验证：成功同提交 + 前置不成立时整体回滚
- [ ] 2.4 `record_work_failed(tenant_id, work_item_id, owner, *, error, max_attempts, backoff_seconds)`：**租约 CAS 前置**；未达上限 → 回 `queued` + `next_attempt_at = now() + backoff` + 清租约；达上限 → `failed` **死信终态** + `finished_at`；两种情形均追加 `work_attempts(outcome='failed')`。`last_error` 落库且经 redaction。验证：退避序列、上限判定、死信终态、审计行各一次
- [ ] 2.5 `sweep_stale_leases(*, grace_seconds=0) -> list[RecoveryRecord]`：把过期 `in_progress` 复位 `queued` + 清租约，**不**递增 `attempt_count`（ADR-3），产出恢复记录。验证：复位正确 + 尝试计数不变 + 未过期不动 + 负向用例 `test_sweep_does_not_exhaust_attempts`
- [ ] 2.6 租户隔离：所有按 id 的推进/查询以 `tenant_id` 过滤，跨租户返回空或失败。验证：跨租户推进/查询用例（spec 租户隔离 requirement）
- [ ] 2.7 `work_attempts` 追加写入（只追加，不删改）：`succeeded`/`failed`/`released`/`recovered`/`redrive` 五类结果各在对应方法内追一行。验证：审计流用例（逐次失败三条独立记录 + redrive 不抹除历史）
- [ ] 2.8 `redrive_work_item(tenant_id, work_item_id, reason, *, operator=None)`：**仅 `failed` 死信可 redrive**（否则 `RedriveNotAllowedError`），追加 `work_attempts(outcome='redrive', error=原因)`、`status='queued'`、`attempt_count=0`、清租约。验证：镜像 `redrive_dead_letter` 的正/负用例
- [ ] 2.9 `release_for_retry(tenant_id, work_item_id, owner, *, delay_seconds)`：维护类延后（`MaintenanceDeferred`）专用——回 `queued` + 延后 `next_attempt_at` + 清租约，**不**计失败、**不**递增 `attempt_count`，追加 `work_attempts(outcome='released')`（ADR-3/ADR-5）。验证：延后不消耗预算 + 审计行 + 可再认领

## 3. `WorkQueueWorker`（ADR-4 / ADR-5）

- [ ] 3.1 `bootstrap/work_queue_worker.py`：`WorkQueueWorkerConfig`（lease TTL 60s / heartbeat 20s / max attempts 5 / backoff 1m,5m,30m,2h,6h / poll 1s / batch 10 / per-tenant 在途 1）、`WorkItemEnvelope`、`WorkHandler` 注入协议。验证：config 校验用例（TTL > heartbeat、backoff 覆盖 max attempts）
- [ ] 3.2 `run()` / `process_once()` / `_process()` / `_heartbeat_loop()`：镜像 `OutboundDeliveryWorker` 的 claim→heartbeat→CAS 结构，含失租时「不写状态」的三处放弃路径。验证：`test_work_queue_state_machine.py` 失租用例 + 心跳到期接管
- [ ] 3.3 tenant lane 接线：`work_kind == interactive` → `TenantLaneRouter.run_interactive`，其余 → `run_maintenance`（ADR-5）；同租户串行、跨租户并发。验证：`tests/control_plane/test_work_queue_tenant_lane.py` 时间线断言（同租户不重叠 + 跨租户并行）
- [ ] 3.4 维护类延后：`MaintenanceDeferred` 不视为失败——释放租约、不递增 `attempt_count`、排程重试（ADR-5 + ADR-4）。验证：延后用例断言尝试计数不变且可再认领
- [ ] 3.5 有界背压：单轮认领 ≤ batch_size；每租户在途 ≤ 1（ADR-4）；过载时明确延后而非堆积。验证：背压用例（就绪项 > batch_size 时不被一次性认领）
- [ ] 3.6 停止语义：`stop()` 只阻止认领新 work，等待在途 handler 收束，**不**取消在途执行（ADR-7）。验证：stop 期间在途 handler 完成、无新认领

## 4. 运行期接线与优雅停止（ADR-7）
- [ ] 4.1 bootstrap 建立 control plane async engine + session factory（复用 `bootstrap/db/engine.py`），构造 `WorkItemRepository` + `WorkQueueWorker`；`storage.backend == "sqlite"` 时跳过（ADR-7）。验证：`tests/control_plane/test_work_queue_wiring.py` 双后端分支用例
- [ ] 4.2 `AppRuntime.start()` 在 `provisioning_worker.start()` 之后启动 worker；`AppRuntime.shutdown()` 的 `_run_cleanup_steps` 加入 worker 停止与 `engine.dispose()`，位置在 `conversation_runtime.shutdown` / `core.stop` 之前。验证：`test_work_queue_wiring.py` 启停顺序断言 + 既有 `tests/test_app_*` 不回归
- [ ] 4.3 配置：新增 `[work_queue]`（`enabled` 默认关闭、参数与 ADR 默认值一致）并接入 `agent/config.py` + `config.example.toml`。验证：配置解析用例（默认与覆盖）
- [ ] 4.4 关闭后遗留项由下次清扫收束：集成用例模拟「在途被中断 → 重启 → 清扫复位 → 重新执行成功」。验证：`test_work_queue_recovery.py` 崩溃恢复 e2e

## 5. 与调用方的接缝

- [ ] 5.1 `WorkHandler` 协议文档化：声明「handler 必须幂等，或把副作用写入放进 `mutate` 同事务」（ADR-6）；非幂等 handler 不得注册。验证：协议 docstring + 一个示范 handler 用例
- [ ] 5.2 既有 `create_work_item()` 与本消费者的衔接：入队侧 `idempotency_key` 唯一约束与认领侧 lease 叠加验证。验证：重复入队 + 单次执行的 e2e 用例

- [ ] 5.3 handler 按 **`flow`** 注册（`flow → WorkHandler` 映射）；`work_kind` 只决定 lane。未注册 flow 的工作项不得执行，应记录并释放（不静默丢弃）。验证：未注册 flow 的负向用例 + spec「工作类型词汇与分派」

## 6. 可观测（并入 C12 §8.1 / E10）（ADR-8）

- [ ] 6.1 按 `tests/fixtures/observability_event_schema.json` 落 work item 的记录点：`claim`（`queue_wait_ms`）、`finish`（`execution_ms`、`status`）、`recovery`（`restart_to_recovered_ms`、`recovery_action`）。验证：契约测试比对 fixture 字段
- [ ] 6.2 内容边界：不记 `payload_json` 原文与 handler 输入输出原文；自由文本过 `core/telemetry/redaction.redact_text`；指标 label 只用白名单维度（不含 `work_id`/`tenant_id`）。验证：负向测试（fixture `forbidden_content_fields` 不出现 + label 白名单校验）
- [ ] 6.3 回填 C12：在 `c12-observability-backup/tasks.md` §8.1 记录本 change 为承接者，并把 checklist observability 条目的对应子项标为已落地。验证：`openspec validate c12-observability-backup`

## 7. 回归与状态

- [ ] 7.1 `pyright --level error`（project + tests 两配置）无新增错误。验证：`openspec/evidence/c15-work-queue-consumer/pyright-*.txt`
- [ ] 7.2 带 PG 全量回归无新增失败：`python scripts/regression.py --start-pg --evidence openspec/evidence/c15-work-queue-consumer/pytest-regression.txt`（PG 前置由 `NEXUS_REQUIRE_PG` 守卫保证，不允许静默 skip）。验证：证据文件
- [ ] 7.3 `openspec validate` 通过；tasks 勾选与 evidence 同步。验证：`openspec status c15-work-queue-consumer`

## 8. 已知边界（不在本 change 内）

- **`OutboundDeliveryWorker` 未接线**：C2 的 delivery worker 同样从未在 `AppRuntime` 中启动；其接线单列后续 change，复用本 change 建立的 engine 接线方式（ADR-7）。
- **既有内存路径未迁移**：`ConversationRuntime._admissions`、`PassiveMessageWorker._lane_queues`、`markdown maintenance` 迁入 `TenantLaneRouter` 仍归 C8/C9 + `TenantRuntimePlan`（尊重 C3 §6 边界）。
- **迁移可安全改写（尚未发布）**：`a7f2c9e4b1d8` 只存在于未推送的 feature 分支，尚未进 `main` / 未部署，因此**直接改写它**（而不是叠加第二个 migration）以保持「一个 change 一个 migration」的仓库惯例（对齐 C2 的七表单迁移）。若该分支已被他人使用，须改为叠加 migration。
- **无 RLS / `security_epoch` fencing**：跨租户防护依赖仓储层 `tenant_id` 过滤 + 负向测试；DB 层防御纵深归后续安全 change。
