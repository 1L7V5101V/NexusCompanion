# C15 durable work queue 消费层与 control plane 运行期接线

> 对应 PILOT_ROADMAP §5.9.11（Ingress/canonical/outbox/delivery transaction）、§5.9.5（tenant admission + 有界队列）、§5.9.6（durable 表与 restart recovery）、§5.9.10（capability 依赖图）、§7.1（必采集数据）、§5.9.9（DB rollout/schema evolution）。
> 上游交付：C2 建表 `background_work_items`（含 `idempotency_key`、CAS `transition_work_item`）但**至今无任何消费者**；C3 交付 `TenantLaneRouter` 但**未接线生产**。
> 编号说明：§5.9.10 的 capability 列表是「至少拆成」，本 change 不是新的 capability 类别，而是**补完 task-02 的 work-item 消费者**并完成 task-02/task-03 共同欠下的**运行期接线**，因此取新序号 C15。

## Why

三处已交付但**不可达**的代码，构成本次变更的全部理由（均已用代码验证，2026-09-20）：

1. **`background_work_items` 是一张只写不读的表。** C2 迁移 `f3c8a9d2e7b4` 建了该表（`queued/in_progress/succeeded/failed/cancelled` + `idempotency_key` 唯一约束），仓储提供 `create_work_item()` / `transition_work_item()`，但全仓**没有任何 worker claim 或执行它**。`grep` 结果：该表的代码引用只出现在 `control_plane_repo.py` 与 model，没有 `claim_batch`。
2. **`background_work_items` 缺 claim 所需的全部列。** DDL 只有 `id/tenant_id/conversation_id/work_kind/status/idempotency_key/payload_json/created_at/updated_at/finished_at`——没有 `attempt_count`、`lease_owner`、`lease_expires_at`、`next_attempt_at`、`last_error`。因此「加一个 claim 方法」不够，必须配一次 expand 迁移。
3. **整个 C2 durable control plane 在生产不可达。** `bootstrap/db/engine.py` 的 `create_engine/create_session_factory` 唯一调用方是 `scripts/import_to_pg.py`；`bootstrap/app.py::AppRuntime.start()` 从不创建 control plane 的 async engine，也从不构造 `OutboundDeliveryWorker`（该 worker 只出现在 tests 与 C2 rollback drill）。C3 的 `TenantLaneRouter` 同样未接线（C3 §6 已记录）。

后果：进程内仍有**四套**队列/执行栈并存，且 durable 的两套都没接上——`passive_worker`/`ConversationRuntime._admissions`/`_lane_queues`（在跑，内存态）、`background_work_items`（不跑）、`outbound_delivery_intents`（不跑）、`TenantProvisioningWorker`（在跑，内存队列）。维护者因此无法判断「持久化到底生效了没有」。

本 change 的目标是**把 work item 这一条边接上并做对**：DB claim + lease + 崩溃恢复 + 同租户串行/跨租户并发 + 有界背压 + effectively-once，并在 bootstrap 里建立 control plane 的 async engine 与 worker 生命周期（delivery 边复用同一接线，单列后续，避免一次改两条边）。

## What Changes

- **DB（expand-only 迁移，新 alembic revision，`down_revision = f3c8a9d2e7b4`）**：给 `background_work_items` 增 `attempt_count`、`lease_owner`、`lease_expires_at`、`next_attempt_at`、`last_error`，并建 claim 扫描索引 `(status, next_attempt_at)`；ORM model 同步。状态词汇**不变**（沿用 `queued/in_progress/succeeded/failed/cancelled`）。
- **`WorkItemRepository`（新，镜像 `DeliveryRepository`）**：`claim_batch()`（`FOR UPDATE SKIP LOCKED`，**每轮每个 tenant 至多 1 条**）、`heartbeat()`（续租，返回 False = 失租）、`record_work_succeeded()`（仅租约仍属 owner 时推进，且与业务副作用写入**同事务**）、`record_work_failed()`（退避/达上限置 `failed`）、`sweep_stale_leases()`（把过期 `in_progress` 复位为 `queued`，即崩溃恢复清扫）。
- **`WorkQueueWorker`（新，`bootstrap/work_queue_worker.py`）**：镜像 `OutboundDeliveryWorker` 的 claim→heartbeat→CAS 结构；把认领到的 work item 按 `tenant_id` 分派到 **tenant lane**（复用 C3 `TenantLaneRouter` 的同租户串行语义），跨租户并发；per-tenant 在途上限与 batch 上限构成有界背压；执行器经**注入的 handler** 接入（本 change 不实现具体 work_kind 的业务逻辑）。
- **运行期接线**：在 bootstrap 创建 control plane async engine + session factory，构造 `WorkItemRepository` 与 `WorkQueueWorker`，随 `AppRuntime.start()` 启动、并加入 `AppRuntime.shutdown()` 的 `_run_cleanup_steps`（照 `TenantProvisioningWorker` 的生命周期模式）。
- **可观测（并入 C12 §8.1 / E10）**：按 `tests/fixtures/observability_event_schema.json` 为 work item 落地 §7.1 字段与事件记录点（claim/start/finish/recovery 的 `work_id`/`tenant_id`/`work_kind`/`enqueued_at`/`started_at`/`finished_at`/`status`/`attempt` 及派生 `queue_wait_ms`/`execution_ms`/`restart_to_recovered_ms`），自由文本字段入库前过 `core/telemetry/redaction.redact_text`。
- **规格**：新增 capability `durable-work-queue`（见 `specs/durable-work-queue/spec.md`）。

## Capabilities

### New Capabilities

- `durable-work-queue`：`background_work_items` 的 durable 消费契约——数据库 lease 认领、崩溃恢复清扫、同租户串行/跨租户并发、有界背压与 overload、副作用与终态同事务的 effectively-once（配合 `idempotency_key`）、运行期接线与优雅停止、租户隔离与 §7.1 可观测记录点。

### Modified Capabilities

- 无。本 change 不修改 `durable-control-plane`（C2）与 `admission-queue-recovery`（C3）的既有行为断言；它只消费 C2 已建的 `background_work_items`，并**复用它**的 lease/attempt 语义模式（不复用其表）。

## Non-Goals（明确不做）

- **不接线 outbound delivery worker**：`OutboundDeliveryWorker` 同样未接线，但那是另一条边（channel 送达），单列后续 change，避免一次改两条边而无法归因。
- **不迁移既有内存路径到 router**：`ConversationRuntime._admissions`、`PassiveMessageWorker._lane_queues`、`MarkdownMemoryMaintenance` 的迁入仍留给 C8/C9 + `TenantRuntimePlan`（尊重 C3 §6 的既有边界）。本 change 只**新增** work item 消费者对 router 的使用。
- **不实现任何具体 work_kind 的业务执行逻辑**：本 change 提供消费者与注入式 handler 接缝；业务 handler（consolidation、optimizer、schedule 等）由各自的 feature change 注册。
- **不改 `bus/queue.py` / 旧单体运行时**。
- **不引入 Redis / 多副本**：Pilot 单进程单 worker（`worker_replicas=1`），扩展评估归 P4 闸门。
- **不做 PG 行级 retention、RLS、`security_epoch` fencing**：分别归 C12 伴随落地与后续安全 change。

## Impact

- **代码**：新增 `bootstrap/work_queue_worker.py`；扩 `bootstrap/db/repository/control_plane_repo.py`（新 `WorkItemRepository`）与 `bootstrap/db/models/control_plane.py`（`BackgroundWorkItemModel` 增列）；改 `bootstrap/app.py`（control plane engine + worker 生命周期）；新增 alembic revision。
- **DB**：`background_work_items` 增 5 列 + 1 索引（expand-only，可 `DROP COLUMN`/`DROP INDEX` 回滚）；无数据回填需求（新列为 DEFAULT/NULL）；不影响既有行。
- **配置**：新增 work queue 参数（lease TTL / heartbeat / max attempts / backoff / poll / batch / per-tenant 在途上限）默认值，接入 `config.toml`。
- **测试**：新增 `tests/control_plane/test_work_queue_state_machine.py`（claim/失租/退避/上限/清扫）、`test_work_queue_tenant_lane.py`（同租户串行 + 跨租户并发 + 背压）、`test_work_queue_wiring.py`（bootstrap 启停）；PG 集成测试沿用 `tests/control_plane/conftest.py` 的 scratch DB。
- **系统/运维**：首次有 durable worker 在 `AppRuntime` 内运行，改变进程启动/停止的时序面（新增一个 task 与一个 cleanup step）；`AppRuntime.shutdown()` 的步骤顺序需要在新步骤前后保持既有不变量。
