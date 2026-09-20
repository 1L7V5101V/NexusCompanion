# ADR: turn 记录与查询日志的存储归属（独立 PG audit store）

> 状态：已定案（2026-09-06），未实现，无代码改动
> 归属：持久化 ownership 与可观测性（PILOT_ROADMAP 差距行「持久化 ownership」；storage-migration S2 前置定案项）
> 同步位置：本记录 / `specs/storage-migration`「turn control plane 数据归属定案」/ `PILOT_ROADMAP` §3 差距表下方决策注 / `docs/tasks/turn-audit-storage-ownership.md`

## 1. 决策

turn 记录与查询日志（`turn_logs`，现 `RoutingTurnLogger` → `{workspace}/logs/{passive,proactive,drift}.db`）**纳入 PostgreSQL 侧，但独立成 audit 库/schema（分库/分表），与 memory/session 事务主库隔离；绝不并入主库 schema，也不长期保留每进程本地 SQLite 唯一副本作为审计源。**

- **目标形态**：`TurnLogger` 保留批量异步 flush，仅把 sink 替换为独立 PG audit 库/schema；每轮以 `turn_id` 贯通 ingress/queue/worker/LLM/store/delivery；写入锚点为 lifecycle `TurnCommitted`；`messages/tool_calls/tools_schema/metadata` 等 JSON 列延续现状；配 retention 与写前 redaction（对齐 C12 observability/privacy）。
- **过渡（现状）**：迁移完成前允许 SQLite-only；在 storage-migration S2 归属声明中 SHALL 显式记录一致性、备份/恢复与回滚策略，不得宣称「PostgreSQL 成为 primary = 全部 session/turn 数据已迁移」。

## 2. 背景与问题（代码事实）

- `TurnLoggerConfig` 只接受 `db_path`（`turn_logging/turn_logger.py:63`），落点不随 `storage.backend` 切换；PG 迁移导入范围只含 `memory_items / sessions / messages / memory_replacements` 及明确纳入的插件表，不含 `turn_logs`。
- `docs/tasks/turn-durability-tool-failure-design.md`（D6）已把 turn 审计留本地 SQLite 记为现状；本 ADR 是它的迁移方向定案，不与之冲突。
- storage-migration spec 原把归属列为「进入 S2 前须定案」的开放需求；PILOT_ROADMAP 差距行把 turn audit 列为仍分散在 SQLite/JSON 的数据，不能宣称 PG 统一 primary data。

问题：水平 Worker / 多副本后无法用 `turn_id` 全局贯通；本地文件不在统一备份与 PITR 范围；redaction/retention 无中心执行点。

## 3. 备选与取舍

| 方案 | 取舍 | 结论 |
|---|---|---|
| 并入 memory/session PG 主库 schema | 追加型、高量、大文本（整轮 prompt/messages）观测数据与事务型 OLTP 争连接/膨胀/备份；排障查询与线上查询混跑 | 不选 |
| 保持本地 SQLite-only 作为审计源 | 多进程不可贯通；不在统一备份/PITR；redaction/retention 无法中心化 | 不选（仅过渡） |
| **独立 PG audit 库/schema（分库/分表）** | 负载隔离、统一备份/PITR、turn_id 贯通、redaction/retention 可中心执行 | **选定** |
| 外部可观测 sink（ClickHouse/ES…） | 新依赖与运行成本超当前阶段收益（同 C0 对 OpenTelemetry 的取舍） | 不在此处做，保留长期候选 |

## 4. 目标形态与边界（做 / 不做）

**做**：独立 PG audit 库/schema；复用 `TurnLogger` 批量异步 flush；`TurnCommitted` 为写入锚点；`turn_id` 贯通；延续 JSON 列结构；retention + 写前 redaction（对齐 C12）。

**不做**：并入 memory/session 主库 schema；每进程本地 SQLite 唯一副本作为审计源；本轮不引入外部可观测栈。

## 5. 过渡声明

迁移完成前保留 SQLite-only；在 storage-migration S2 归属声明中显式记录一致性、备份/恢复与回滚策略；不得把「PostgreSQL 成为 primary」笼统解释为所有 session/turn 数据已迁移。
