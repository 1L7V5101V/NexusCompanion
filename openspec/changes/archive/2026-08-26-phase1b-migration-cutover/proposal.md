# Phase 1B 数据迁移工具与主数据源切换（C1B + C1C）

## Why

C1 Storage Foundation（SQLite/PG 双 adapter、tenant 贯穿、pool + bounded executor、provisioning 控制面）已 merge（`0a83314d`），但生产仍以 SQLite 为主数据源。现有 `scripts/import_to_pg.py` 逐行 insert（万级慢）、无断点续传、无机器可读校验；且无迁移校验工具（根目录 `verify_migration.py` 为 opencode.db 检查脚本，与本次迁移无关）。5000 用户目标要求 PostgreSQL 成为会话、记忆与交付状态的主数据源；必须先让 SQLite → PG 的迁移工具可恢复、可校验、证据可复现，再按 S0-S4 状态机切换到 PG primary，才能安全承载多用户生产。

## What Changes

- **批量导入**：`import_to_pg.py` 从逐行 insert 改为批量 COPY / 批量 insert，支持可配置 batch、进度输出、断点续传（checkpoint/resume）与幂等重跑；补齐所有 Phase 1 目标表（`memory_items`、`sessions`、`messages`、`memory_replacements` 及明确纳入本阶段的插件数据）。
- **显式 tenant mapping**：导入必须要求显式 tenant mapping；禁止无提示把所有历史数据导入 `default`（沿用决策 C 约束）。
- **机器可读校验**：`verify_migration.py` 提供逐表行数、关键字段 hash、引用完整性（FK/复合键）与语义抽样（向量 top-k、关键词搜索、session seq），输出机器可读结果并保存为迁移证据。
- **S0-S4 状态机**：实现主计划第 8 节迁移状态机——S0 SQLite primary → S1 snapshot import + shadow validation → S2 PG primary（保留审计窗口）→ S3 PG stable → S4 旧实现清理；每个状态明确 primary、允许写路径、对账方式、进入/退出条件。不采用无主次的简单双写；S2 之后无 PG→SQLite 反向同步时不承诺无损回切 SQLite。
- **staging cutover + 对账**：在 staging 完成 S0→S2 切换、对账与回滚演练。
- **PITR 恢复与回滚演练**：完成 PITR 恢复演练并记录 runbook；回滚边界按状态机明确定义。

## Capabilities

### New Capabilities
- `storage-migration`: SQLite → PostgreSQL 数据迁移工具（批量 COPY、断点续传、显式 tenant mapping、机器可读校验）与 S0-S4 主数据源切换状态机（含 staging cutover、对账、PITR 恢复与回滚演练）。

### Modified Capabilities
- 无（`scaling-governance` 无 requirement 变更；迁移行为是新增能力，不改既有治理规则）。

## Impact

- **代码**：`scripts/import_to_pg.py`、`verify_migration.py`（或迁移为 `scripts/migrate/` 模块）；可能新增 storage 批量写入 seam（现有接口以单条/批量 `upsert_*` 为主，COPY 需要 backend 级入口）。
- **配置**：迁移相关配置（batch size、checkpoint 路径、tenant mapping 文件、cutover 状态持久化）新增到 config。
- **依赖**：无新增第三方依赖预期（COPY 走现有 psycopg）；校验 hash 用 stdlib。
- **系统/运维**：S0-S4 状态机工具与 runbook；迁移证据入库（`openspec/evidence/`）；staging 环境 PostgreSQL 需可用（复用 `docker/debug/docker-compose.yml`）。
- **测试**：迁移工具幂等/断点续传/校验正确性测试；S0-S4 状态机单元与 staging 演练脚本。
