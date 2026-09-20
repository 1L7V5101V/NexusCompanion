# Turn 审计日志存储归属：决策记录

日期：2026-09-06
状态：已定案（决策记录）；未实现，无代码改动。与 openspec 主文档同步（见文末）。

## 1. 决策

turn 记录与查询日志（每轮 turn 日志，现 `RoutingTurnLogger` → `{workspace}/logs/{passive,proactive,drift}.db`）**纳入 PostgreSQL 侧，但独立成 audit 库/schema（分库/分表），与 memory/session 事务主库隔离**；不并入主库 schema，也不长期保留每进程本地 SQLite 唯一副本作为审计源。

- 目标形态：`TurnLogger` 批量异步 flush 保留，sink 换独立 PG audit 库/schema；写入锚点 = lifecycle `TurnCommitted`；`turn_id` 贯通多 hop；`messages/tool_calls/metadata` 等 JSON 列延续；配 retention + 写前 redaction（对齐 C12）。
- 过渡：迁移完成前 SQLite-only 可保留；storage-migration S2 归属声明须显式记录一致性/备份/恢复/回滚策略，不得宣称「PG 成为 primary = 全部 turn 数据已迁移」。

## 2. 为何独立分库而不并入主库

turn 日志是追加型、高量、大文本（整轮 prompt/messages），与 memory/session 事务负载混跑会争资源、膨胀、污染备份与 PITR。独立分库/分表获得：负载隔离 + 统一备份/恢复 + 多 Worker/多副本下按 `turn_id` 全局贯通 + 中心化 redaction/retention 执行点。

本决策与 `docs/tasks/turn-durability-tool-failure-design.md` 的 D6「turn 审计暂留本地 SQLite」不冲突：D6 是现状，本文是迁移方向定案。

## 3. 同步位置

- `openspec/records/turn-audit-storage-ownership.md`（ADR 正文）
- `openspec/specs/storage-migration/spec.md`（Requirement: turn control plane 数据归属定案）
- `openspec/PILOT_ROADMAP.md`（§3 差距表下方决策注）
