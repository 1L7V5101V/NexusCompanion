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

- [x] 2.1 `claim_batch(owner, *, batch_size, lease_ttl_seconds)`：单语句原子认领（`FOR UPDATE SKIP LOCKED`，`status→in_progress`，写租约，**不修改 `attempt_count`**）。**due 判据 = `queued` 且到期 OR stale `in_progress`**（(B) 定案：**不含 `failed`、不含 `attempt_count` 门槛** ⇒ 死信结构性不被认领、崩溃不计入预算）。含**两个 per-tenant 条件**：在途互斥 + 轮内去重（ADR-4，形状已在真 PG 验证）。验证：`tests/control_plane/test_work_queue_state_machine.py` 全路径 + 并发认领只成功一次 + `test_claim_skips_tenant_with_active_lease` + `test_failed_terminal_not_claimed`
- [x] 2.2 `heartbeat(tenant_id, work_item_id, owner, *, lease_ttl_seconds) -> bool`：续租，返回 False = 失租。验证：续租成功/失租两向用例；lease 到期时间与 claim 同源（DB 时钟）
- [x] 2.3 `record_work_succeeded(tenant_id, work_item_id, owner, *, mutate)`：租约 CAS 前置下推进 `succeeded` + `finished_at` + 清租约，并与 `mutate(session)` 的副作用写入**同事务**（ADR-6）。验证：成功同提交 + 前置不成立时整体回滚
- [x] 2.4 `record_work_failed(tenant_id, work_item_id, owner, *, error, max_attempts, backoff_seconds, started_at=None)`：**租约 CAS 前置**；**先递增 `attempt_count`（全仓唯一递增点）**，再决定终局：未达上限 → 回 `queued` + `next_attempt_at = now() + backoff[attempt_count-1]` + 清租约；达上限 → `failed` **死信终态** + `finished_at`；两种情形均追加 `work_attempts(outcome='failed')`。`last_error` 落库且经 redaction。验证：退避序列、上限判定（恰好 max_attempts 次失败后判死）、死信终态、审计行各一次
- [x] 2.5 `sweep_stale_leases(*, limit=100) -> list[dict]`：把过期 `in_progress` 复位 `queued` + 清租约，**不碰 `attempt_count`**（ADR-3：崩溃不消耗预算），并为每条追加 `work_attempts(outcome='recovered')`，返回可作为恢复记录的字段（含 `restart_to_recovered` 所需时间戳）。验证：复位正确 + `attempt_count` 不变 + 未过期不动 + 负向用例 `test_sweep_does_not_consume_budget`
- [x] 2.6 租户隔离：所有按 id 的推进/查询以 `tenant_id` 过滤，跨租户返回空或失败。验证：跨租户推进/查询用例（spec 租户隔离 requirement）
- [x] 2.7 `work_attempts` 追加写入（只追加，不删改）：`succeeded`/`failed`/`released`/`recovered`/`redrive` 五类结果各在对应方法内追一行。验证：审计流用例（逐次失败三条独立记录 + redrive 不抹除历史）
- [x] 2.8 `redrive_work_item(tenant_id, work_item_id, reason, *, operator=None)`：**仅 `failed` 死信可 redrive**（否则 `RedriveNotAllowedError`），追加 `work_attempts(outcome='redrive', error=原因)`、`status='queued'`、`attempt_count=0`、清租约。验证：镜像 `redrive_dead_letter` 的正/负用例
- [x] 2.9 `release_for_retry(tenant_id, work_item_id, owner, *, delay_seconds)`：维护类延后（`MaintenanceDeferred`）专用——回 `queued` + 延后 `next_attempt_at` + 清租约，**不**计失败、**不碰 `attempt_count`**，追加 `work_attempts(outcome='released')`（ADR-3/ADR-5）。验证：延后不消耗预算 + 审计行 + 可再认领

> §2 证据：真 PG18 上用**真实仓储代码**跑 43 项语义验证全 PASS（含 ADR-4 条件①反例、
> 同事务副作用回滚、**连续 7 次崩溃不消耗预算且不判死**、失租禁写、租户隔离、审计流只追加）。
> 见 [`work-queue-repo-verification.md`](../../evidence/c15-work-queue-consumer/work-queue-repo-verification.md)
> （+ 可复跑脚本与原始输出）；仓库内正式回归版 = `tests/control_plane/test_work_queue_state_machine.py`。

## 3. `WorkQueueWorker`（ADR-4 / ADR-5）

- [x] 3.1 `bootstrap/work_queue_worker.py`：`WorkQueueWorkerConfig`（lease 60s / heartbeat 20s / max attempts 5 / backoff 1m,5m,30m,2h,6h / poll 1s / batch 10 / maintenance acquire 5s / release delay 60s）、`WorkItemEnvelope`、`WorkHandler` **两段式**注入协议（ADR-6 细化）、`flow → handler` 映射。验证：`tests/test_work_queue_worker.py`（config 冻结默认值 + 不变量）
- [x] 3.2 `run()` / `process_once()` / `_process()` / `_run_handler()` / `_heartbeat_loop()`：镜像 `OutboundDeliveryWorker` 的 claim→heartbeat→CAS 结构，含失租时「不写状态」的四处放弃路径；lane 协程**惰性创建**（提前建会在延后/关闭时留下未 await 的协程，`-W error` 下直接失败）；**轮询级异常隔离**（`claim_batch` 抛错只记日志 + 线性退避重试，绝不逃出 `run()`——`_run_primary_tasks` 把 runtime task 异常当致命，会取消同级任务并退出进程）。验证：失租禁写（成功/失败两向）+ execute/persist 失败计入业务失败 + 轮询异常不逃逸且恢复后继续消费
- [x] 3.3 tenant lane 接线：`work_kind == interactive` → `TenantLaneRouter.run_interactive`，其余 → `run_maintenance`（ADR-5，**复用** C3 router，未新造 lane）；同租户串行、跨租户并发。验证：`tests/test_work_queue_worker.py` 时间线断言（同租户 `max_active==1` + 跨租户阻塞时另一租户先完成）
- [x] 3.4 维护类延后：`MaintenanceDeferred` 不视为失败——`release_for_retry` 释放租约、不动 `attempt_count`、留 `released` 审计行、排程重试（ADR-5）。验证：延后用例（interactive 占优时维护类被释放，`failed` 为空）
- [x] 3.5 有界背压：单轮认领 ≤ `batch_size`（透传仓储）；每租户在途 ≤ 1 由认领 SQL 保证（ADR-4）。验证：`batch_size` 透传断言（owner/lease_ttl 一并校验）
- [x] 3.6 停止语义：`stop()` 置停止标志 + `router.close()`（拒绝新 work），在途 handler 继续收束；`process_once()` 在 router 关闭后直接返回 0、不再认领（ADR-7）。验证：`stop()` 后 `router.closed` 为真、`process_once()==0` 且无 claim 调用

## 4. 运行期接线与优雅停止（ADR-7）
- [x] 4.1 新增 `bootstrap/work_queue.py`：`build_work_queue_runtime()` 建立 control plane async engine + session factory（复用 `bootstrap/db/engine.py`）并构造 `WorkItemRepository` + `WorkQueueWorker`；连接串驱动从 `storage.postgres_url`（sync psycopg）转 `+asyncpg`；非 postgres 后端跳过；**未注册 handler 时 fail-fast**（否则全部 work item 会被判失败进死信）。验证：`tests/test_work_queue_wiring.py`（驱动转换幂等 / 默认关闭 / 非 postgres 跳过 / 未注册 handler 抛错 / engine+worker 装配与配置映射）
- [x] 4.2 `AppRuntime.start()` 在 `provisioning_worker.start()` 之后启动 worker（**独立 task，不放入 `self.tasks`**——那会与 `_run_primary_tasks` 的一损俱损语义耦合）；`shutdown()` 的 `_run_cleanup_steps` 新增 `work_queue.drain_and_stop`，位置在 **`runtime_tasks.cancel` 之前**（否则会在 handler 执行中取消它），并在其中 `engine.dispose()`；异常经 `_work_queue_done` 回调大声记录。验证：既有测试不回归（`pytest -k 'config or app_runtime or bootstrap'` 85 passed / 2 skipped）
- [x] 4.3 配置：新增 `[agent.work_queue]`（`enabled` 默认 false、参数与 ADR 冻结默认一致）并接入 `agent/config_models.py`（`WorkQueueConfig`）+ `agent/config.py::_load_work_queue_config` + `config.example.toml`；**加载期**校验 `lease_ttl > heartbeat`、`max_attempts ≤ 退避档数`、error backoff 关系。验证：配置默认值/覆盖/非法值用例
- [ ] 4.4 关闭后遗留项由下次清扫收束：集成用例模拟「在途被中断 → 重启 → 清扫复位 → 重新执行成功」。验证：`test_work_queue_recovery.py` 崩溃恢复 e2e → **待 task 7.2 的 PG 环境**；等价语义已在真 PG 仓储验证中覆盖（连续 7 次崩溃仍 `queued`、`attempt_count=0`、可认领 + 清扫复位），见 [`work-queue-repo-verification.md`](../../evidence/c15-work-queue-consumer/work-queue-repo-verification.md)

## 5. 与调用方的接缝

- [x] 5.1 `WorkHandler` 协议文档化：**两段式**（`execute` 长活不写库 / `persist` 与终态同事务）+ 声明「两段合起来必须幂等或可重放」（ADR-3 边界 / ADR-6 细化）。验证：协议 docstring + 两段式调用顺序用例
- [ ] 5.2 既有 `create_work_item()` 与本消费者的衔接：入队侧 `idempotency_key` 唯一约束与认领侧 lease 叠加验证。验证：重复入队 + 单次执行的 e2e 用例

- [x] 5.3 handler 按 **`flow`** 注册（`flow → WorkHandler` 映射）；`work_kind` 只决定 lane。未注册 flow 的工作项**不得执行**：按业务失败记录（`record_work_failed`，错误信息含「未注册 flow」），使其最终进死信可见并可 redrive——而非静默丢弃，也不是无限释放把审计流写爆（对任务原文「释放」的有意修正）。验证：未注册 flow 用例断言 `failed` 恰 1 条且含「未注册 flow」

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
