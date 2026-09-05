# C2 durable control plane：ingress/inbox + turn/work + outbox/delivery

> 对应任务计划：`openspec/openspec-tasks-bundle/task-02-durable-control-plane.md`（PILOT_ROADMAP §5.9.10 第 2 项）。
> 输入的已冻结决策（§5.9.6 / §5.9.9 / §5.9.11 / §10 DECIDED）不在此重复论证，design.md 逐条引用。

## Why

Pilot 的「重启不丢数据、模型生成完成 ≠ channel 已送达、可补拉/可重投」承诺依赖三个明确事务边界（§5.9.11）：入站接受、执行完成、独立 delivery ack。当前代码的 inbound/outbound 队列与 Passive lane 都是无界进程内 `asyncio.Queue`（`bus/queue.py`、`bootstrap/passive_worker.py`），重启丢 backlog；turn 状态虽可持久化但 PG session/message 与 SQLite `turn_audit.db` 双存储；出站只有 dispatcher 内 2s 重试一次后即宣告消息丢失，没有 delivery record、ack 或幂等键。C1 已交付 canonical 会话/消息与 per-conversation sequence，C2 在其上落地 durable control plane，是 C4（dev WebChat durable 闭环）、C10（跨端同步）与 P1GATE（公网 durable 承诺）的前置。

## What Changes

- **P-1 设计冻结**：三事务边界、7 张表 DDL 与约束命名、幂等双键（Telegram source id / WebChat `client_message_id`）、outbox lease/heartbeat/dead-letter 语义、`sent` 仅由 ack 推进的状态机、「delta 不承诺 durable」边界（见 `design.md`，ADR 编号 ADR-1..ADR-8）。
- **DB schema**：新增 Alembic migration（revises `c4d8f2a6e9b3`），创建 `inbox_records` / `message_deduplication_keys` / `turns` / `tool_calls` / `background_work_items` / `outbound_delivery_intents` / `delivery_attempts` 七表，含双幂等键部分唯一索引、状态 CHECK、FK(RESTRICT) 与 claim/扫描索引。
- **代码**：
  - `bootstrap/db/models/control_plane.py` — 七表 SQLAlchemy 模型；
  - `bootstrap/db/repository/control_plane_repo.py` — 入站接受事务（dedupe + canonical user message + inbox + queued turn/work 原子提交）、执行完成事务（final assistant message + turn terminal + outbox intent 原子提交）、delivery 仓储（lease 认领/heartbeat/attempt 记录/状态推进/redrive）；
  - `bootstrap/delivery_worker.py` — `OutboundDeliveryWorker`：数据库 lease 认领 outbox，单次 attempt 记录 provider receipt/error/时间戳，退避重试至 `dead_letter`，支持管理员 redrive；发送通道以注入的 callback 接入（本 change 不改接既有 channel dispatcher）。
- **契约 fixture**：`tests/fixtures/control_plane_idempotency.json` — 入站幂等双键正/负用例，供 C4/C10 复用。
- **测试**：PG 集成测试（三事务原子性/回滚无半写入、delivery 状态机全路径、`sent`-only-on-ack 负向、重启重放不重复 final、双键幂等注入、stale lease 接管、dead-letter redrive）+ 静态防回归测试（control plane 模块无 SQLite fallback / 无 `DEFAULT_TENANT`）。
- **证据**：`openspec/evidence/c2-durable-control-plane/` — 可复现脚本 + 原始输出。

## Capabilities

### New Capabilities

- `durable-control-plane`：入站接受事务原子性、双幂等键去重、执行完成事务原子性、outbox delivery 状态机（`pending/attempting/sent/failed/dead_letter` + lease + attempt + provider receipt）、重启重放只补投递不重新生成 final、`complete_inbound` 等价的 inbox 收束语义。

### Modified Capabilities

- 无（`canonical-identity` 主 spec 不改 requirement：C2 只**借用** canonical conversation + sequence 写入 seam，不改其行为契约）。

## Non-Goals（明确不做）

- **不触碰 admission/overload（C3）**：不做 tenant lane、有界队列、overload policy、重启补偿扫描的调度策略；`PassiveMessageWorker` / `MessageBus` 现有进程内路径保持现状（接线归 C4/C3）。
- **不触碰 WebChat 协议与前端（C4）**：不改 `web_chat_protocol.py`、`frontend/chat/`；C2 只为其提供 durable inbox/outbox seam。
- **不触碰 Telegram binding（C10）**：幂等键中的 `source_identity_id` 只是原文标识列，不建 binding 表。
- **不做 auth/provisioning（C5）**：不建 access_tokens / auth_sessions；dev 身份沿用 C1 seed。
- **不做 tool audit 与工具幂等键表（C7）**：`tool_calls` 只记录调用与终态；`tool_audit_events` / `tool_idempotency_keys` 归 C7。
- **不重写既有出站路径**：`MessageBus.dispatch_outbound` 的单体兼容行为保持不变；delivery worker 是新的独立 seam，切换在后续 change 按实现地图执行。
- **不导入旧单体数据**：无任何 SQLite 读写路径（§10 DECIDED）。
- **流式 delta 不进入恢复承诺**（§5.9.11）：delta 只存在于在线连接，本 change 不持久化 delta。

## Impact

- **代码**：新增 `bootstrap/db/models/control_plane.py`、`bootstrap/db/repository/control_plane_repo.py`、`bootstrap/delivery_worker.py`、`alembic/versions/<rev>_c2_durable_control_plane.py`；`alembic/env.py` 增加模型导入。
- **配置**：本 change 不新增 config.toml 项（lease/退避等参数以 `DeliveryWorkerConfig` 冻结默认值提供，bootstrap 接线时归 C4/C3 暴露配置）。
- **依赖**：无新增第三方依赖。
- **测试**：新增 `tests/control_plane/`（PG 集成，PG 不可用时 skip）+ `tests/test_control_plane_contract.py`（静态契约，无 PG 依赖）。
- **系统/运维**：delivery 死信需管理员查询与人工 redrive（API 由本 change 的仓储层提供，admin UI/入口归后续 change）。
