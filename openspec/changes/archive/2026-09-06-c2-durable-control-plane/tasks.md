# C2 durable control plane — 实施任务

> 对应 `openspec/openspec-tasks-bundle/task-02-durable-control-plane.md`；证据统一落 `openspec/evidence/c2-durable-control-plane/`。
> 管理闭环：任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新 task-02 / PILOT_ROADMAP_PROJECT_CHECKLIST 状态。
> 分支/worktree：`feature/c2-durable-control-plane` @ `D:\.Projects\NexusCompanion-c2`（基于 `main`@`632d3006`）。

## 1. 实现准备

- [x] 1.1 本地 PostgreSQL 测试实例就绪（便携 PG 17.5 @ localhost:5433，`nexus` 库含 vector + pg_trgm 扩展；worktree 内 `.venv` 可导入 sqlalchemy/psycopg）。验证：psycopg 连接成功 + `select version()` 输出存 evidence
  - 证据：`openspec/evidence/c2-durable-control-plane/env-localpg.txt`

## 2. 模型与 Migration（P0 基础）

- [x] 2.1 `bootstrap/db/models/control_plane.py`：`MessageDeduplicationKeyModel` / `InboxRecordModel` / `TurnModel` / `ToolCallModel` / `BackgroundWorkItemModel` / `OutboundDeliveryIntentModel` / `DeliveryAttemptModel` 七模型，约束命名按 design.md ADR-8（部分唯一索引用 `Index(unique=True, postgresql_where=...)`）。验证：pyright 通过；模型可被 alembic env 导入
- [x] 2.2 Alembic migration（revises `c4d8f2a6e9b3`）：七表 + 部分唯一索引 + FK(RESTRICT) + CHECK + 索引；seed `not_applicable`（design.md §3 注记）；downgrade 删表。验证：空 PG `upgrade head` 成功、`pg_constraint`/`pg_indexes` 断言（含两个部分唯一索引谓词）、downgrade→upgrade 循环
  - 证据：`tests/control_plane/test_migration.py` + `openspec/evidence/c2-durable-control-plane/pytest-migration.txt`
- [x] 2.3 `alembic/env.py` 注册新模型导入。验证：pyright 对模型/迁移零新增错误（`evidence/.../pyright.txt`）；`upgrade head` 后表结构与模型一致（集成测试断言约束/索引）

## 3. Repository 三事务边界

- [x] 3.1 `bootstrap/db/repository/control_plane_repo.py` — `IngressRepository.accept_inbound()`（T1：dedupe `ON CONFLICT DO NOTHING` + 冲突回查返回既有身份 → canonical user message（C1 sequence 取号）→ inbox(accepted) → turn(queued) → 可选 background work items，单事务）+ `mark_inbox_processed()`（幂等收束）。验证：单元/集成测试覆盖
- [x] 3.2 `TurnControlRepository` — `transition_turn()`（expected_status CAS）、`complete_turn_with_delivery()`（T2：assistant final message 取号 + turn 终态 + pending intent 单事务，幂等键默认 `msg:<message_id>`）、`fail_turn()`（失败终态无 intent）、`record_tool_call()/finish_tool_call()`、后台工作项状态推进。验证：事务原子性/回滚测试
  - 证据：`openspec/evidence/c2-durable-control-plane/pytest-accept-complete.txt`
- [x] 3.3 `DeliveryRepository` — `claim_batch()`（`FOR UPDATE SKIP LOCKED`，认领 pending/failed 到期 + stale attempting 接管，`attempt_count+1` + lease 写入）、`heartbeat()`（续租；0 行 = 失租）、`record_attempt_sent()/record_attempt_failed()`（attempt 行 + 状态推进 + 退避/`dead_letter`）、`redrive_dead_letter()`（追加 redrive 记录 + 复位，非 dead_letter 拒绝）、租户过滤查询。验证：状态机全路径测试
  - 证据：`openspec/evidence/c2-durable-control-plane/pytest-delivery.txt`

## 4. Delivery worker

- [x] 4.1 `bootstrap/delivery_worker.py` — `DeliveryWorkerConfig`（frozen：`lease_ttl=60s`、`heartbeat=20s`、`max_attempts=5`、`backoff=[60,300,1800,7200,21600]`、`poll_interval=1s`、`batch_size=10`）+ `OutboundDeliveryWorker`（注入 `send_callback`；claim → heartbeat 任务 → 发送 → sent/failed 推进；失租不得写 sent）。验证：pyright 通过；worker 集成测试（含双 worker 接管场景）
  - 证据：`openspec/evidence/c2-durable-control-plane/pytest-worker.txt`

## 5. 并发与负向测试（验收核心）

- [x] 5.1 事务回滚测试：T1/T2 各构造中途失败（约束违规）→ 断言无半写入（七表零残留 + sequence 计数器无空洞）。验证：pytest 断言
- [x] 5.2 幂等双键重复注入测试：同 `client_message_id`（WebChat）/ 同 source identity + source message id（Telegram）→ 第二次返回既有身份、零新写入；双键并存互不干扰；并发同键注入只产生一条。验证：pytest 断言
  - 证据：`tests/control_plane/test_accept_transaction.py` + `openspec/evidence/c2-durable-control-plane/pytest-idempotency.txt`
- [x] 5.3 状态机与负向测试：无 ack 不进 sent；退避排程正确；达到上限进 dead_letter；stale lease 接管；失租不得写 sent；非 dead_letter redrive 拒绝。验证：pytest 断言（全路径覆盖）
  - 证据：`tests/control_plane/test_delivery_state_machine.py`
- [x] 5.4 重启重放测试：T2 提交后模拟重启（新 worker 实例）→ 只补投未确认 intent、final assistant message 始终一条；已 sent intent 重启后无新 attempt。验证：pytest 断言
  - 证据：`tests/control_plane/test_restart_replay.py` + `openspec/evidence/c2-durable-control-plane/pytest-restart-replay.txt`
- [x] 5.5 「模型完成 ≠ 已送达」分离测试：投递失败（failed/dead_letter）时 final message 仍可按序补拉且内容一致；attempt/receipt/时间戳独立可查。验证：pytest 断言

## 6. 契约 fixture 与静态防回归

- [x] 6.1 `tests/fixtures/control_plane_idempotency.json`：双键正/负用例 fixture（Telegram source 键 / WebChat client_message_id 键），供 C4/C10 复用。验证：`tests/test_control_plane_contract.py` 消费该 fixture
- [x] 6.2 静态契约测试：扫描 `bootstrap/db/models/control_plane.py` + `bootstrap/db/repository/control_plane_repo.py` + `bootstrap/delivery_worker.py` 无 sqlite 引用、无 `DEFAULT_TENANT` 引用；不 import 单体 `bus`/`session` 模块。验证：pytest 通过（无 PG 依赖，CI 可跑）
  - 证据：`openspec/evidence/c2-durable-control-plane/grep-no-sqlite-fallback.txt` + `pytest-static-contract.txt`

## 7. 回滚演练与验收回归

- [x] 7.1 Create→Verify→Enable→rollback 演练：scratch DB `upgrade head`（Create）→ 校验约束/空基线（Verify）→ 模拟入口开放与 T1/T2 写入（Enable）→ 关闭入口 + 停 worker、**保留 PG 数据**断言行数与状态不变（rollback drill）。验证：演练脚本可重跑，记录存 evidence
  - 证据：`rollback-drill.md` + `rollback_drill.py` + `rollback-drill-output.txt`
- [x] 7.2 PR diff 范围检查：未触碰 canonical identity 表（C1）/ admission（C3）/ WebChat 协议（C4）/ Telegram binding（C10）/ auth（C5）表与模块；`MessageBus.dispatch_outbound` 行为未变。验证：`git diff --stat` 记录 + 范围声明存 evidence
- [x] 7.3 回归：`pyright --level error`（project + tests 两配置）、`pytest -q -W error tests/`（全量，PG 集成在 5433 可用时执行）。验证：输出存 evidence，无新增失败
  - 证据：`pytest-regression.txt` + `pyright.txt`
- [x] 7.4 状态更新：task-02 状态置 `in_progress`（本 change 生效后）；全部 evidence 齐全且合并后按 §8 置 `verified`；`openspec validate` 通过
