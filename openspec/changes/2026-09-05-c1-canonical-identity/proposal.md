# C1 canonical identity + PostgreSQL conversation/message 基础

> 对应任务计划：`openspec/openspec-tasks-bundle/task-01-canonical-identity.md`（PILOT_ROADMAP §5.9.10 第 1 项）。
> 输入的已冻结决策（§5.9.2 / §5.9.9 / §10 DECIDED）不在此重复论证，design.md 逐条引用。

## Why

Pilot 的所有下游能力（durable control plane、WebChat 闭环、auth/provisioning、Telegram binding、tenant 隔离）都依赖一个服务端派生的身份链 `account_id → tenant_id → canonical_conversation_id` 与 per-conversation 0-based canonical message stream。当前代码仍是 `channel:chat_id` 派生（`infra/storage/tenancy.py`、`bus/events.py.session_key`），message 序号依赖应用层 `next_seq` 两步提交（`session/store.py`），不满足 §5.9.2 的并发原子分配要求。C1 是锚定根 change：没有它，C2/C4/C5/C9/C10/C14 无表可依、无 seam 可接。

## What Changes

- **P-1 设计冻结**：canonical identity 派生链、三表 DDL 与约束命名、sequence 事务分配语义、Create→Verify→Enable rollout/rollback 语义、「旧单体 SQLite 数据不迁入」声明（见 `design.md`，ADR 编号 ADR-1..ADR-7）。
- **DB schema**：新增 Alembic migration，在 PostgreSQL 创建 `test_accounts` / `canonical_conversations` / `canonical_messages` 三表，含唯一约束（`test_accounts.tenant_id` 唯一、`canonical_conversations.tenant_id` 唯一、`canonical_messages.(conversation_id, sequence)` 唯一）、状态 CHECK 约束、外键、索引与 dev seed。
- **代码**：
  - `bootstrap/db/models/canonical.py` — 三张表的 SQLAlchemy 模型；
  - `bootstrap/db/repository/canonical_repo.py` — 账号/会话/消息 repository + per-conversation sequence 事务原子分配（单事务内 `UPDATE ... RETURNING` 取号 + INSERT 消息）；
  - `bootstrap/identity.py` — canonical identity resolver（可信 principal → account/tenant/canonical conversation），fail-closed，无 binding 拒绝、禁止 DEFAULT_TENANT 回退。
- **契约 fixture**：`tests/fixtures/canonical_identity_chain.json` — 派生链正/负用例，供 C2/C4/C5/C9/C10/C14 复用。
- **测试**：PG 集成测试（migration 在空 PG upgrade head、并发 sequence 分配、唯一约束、负向拒绝、跨 conversation 独立编号）+ 静态防回归测试（canonical 路径无 SQLite fallback）。
- **证据**：`openspec/evidence/c1-canonical-identity/` — 可复现脚本 + 原始输出（migration/并发/负向/grep/rollback drill）。

## Capabilities

### New Capabilities

- `canonical-identity`：服务端派生 `account_id → tenant_id → canonical_conversation_id` 身份链、per-conversation 0-based `BIGINT` sequence 的 PostgreSQL 事务原子分配、无 binding fail-closed 拒绝语义。

### Modified Capabilities

- 无（既有 `scaling-governance`、storage 相关 spec 不改 requirement；旧 `channel:chat_id` 单体路径本 change 不动，按 §3.4 保留兼容）。

## Non-Goals（明确不做）

- **不导入旧单体数据**：SQLite session/message/memory 不迁移、不 dry-run、不生成 mapping 清单；无任何「PG 查询失败回退 SQLite」或双写路径（§10 DECIDED）。
- **不触碰 C2 边界**：不建 inbox_records / message_deduplication_keys / turns / tool_calls / background_work_items / outbound_delivery_intents / delivery_attempts 表；入站去重幂等键归 C2。
- **不触碰 C3 边界**：不做 admission lane / 有界队列 / overload policy；`tenant_id_for_channel()` 仍是 C3 的临时 admission key（E9 弱对齐），本 change 不改它。
- **不做 C5 auth**：不建 access_tokens / auth_sessions / provisioning readiness；`test_accounts` 只含 C1 所需最小字段（status 枚举冻结自 §5.9.9）。
- **不做 C10 Telegram binding 表**：binding 语义只在 design/spec 中作为 resolver 上游输入被引用。
- **不改旧单体运行时**：`session/manager.py`、`infra/storage/tenancy.py`、`bus/events.py` 保持现状；旧 SQLite 模式可独立继续使用（§5.9.2）。

## Impact

- **代码**：新增 `bootstrap/db/models/canonical.py`、`bootstrap/db/repository/canonical_repo.py`、`bootstrap/identity.py`、`alembic/versions/<rev>_c1_canonical_identity.py`；`alembic/env.py` 增加模型导入。
- **配置**：无新配置项（DB URL 复用现有 `DatabaseConfig` / `DATABASE_URL`）。
- **依赖**：无新增第三方依赖。
- **测试**：新增 `tests/canonical_identity/`（PG 集成，PG 不可用时 skip）+ `tests/test_canonical_identity_contract.py`（静态契约，无 PG 依赖）。
- **系统/运维**：首次启用按 Create→Verify→Enable；rollback = 关闭 Pilot 入口 + 保留 PG 数据（见 design.md §Rollback）。
