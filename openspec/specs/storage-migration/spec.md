# storage-migration Specification

## Purpose

定义 SQLite → PostgreSQL 数据迁移工具（批量导入、断点续传、显式 tenant mapping、机器可读校验）与 S0-S4 主数据源切换状态机的行为契约，确保迁移可恢复、可校验、证据可复现，并让 PostgreSQL 安全地成为生产主数据源。

## Requirements

### Requirement: 迁移工具支持批量导入与断点续传

迁移工具 SHALL 以可配置的批量大小将 SQLite 数据导入 PostgreSQL，支持进度输出；导入中断后 SHALL 从最近的 checkpoint 续传；对同一源重复运行 SHALL 幂等，不产生重复或丢失数据。

#### Scenario: 批量导入中断后恢复

- **WHEN** 导入在完成部分批次后中断
- **THEN** 重新运行迁移工具从 checkpoint 续传剩余数据，最终各表行数与源一致

#### Scenario: 幂等重跑

- **WHEN** 对同一 SQLite 源数据再次运行完整导入
- **THEN** 目标表行数与首次导入一致，无重复记录

### Requirement: 迁移覆盖全部 Phase 1 目标表

导入 SHALL 覆盖 `memory_items`、`sessions`、`messages`、`memory_replacements` 以及明确纳入本阶段的插件数据表；每张表导入后的行数与 SQLite 源一致。

#### Scenario: 逐表行数一致

- **WHEN** 导入完成并逐表统计 PostgreSQL 行数
- **THEN** 每张目标表的行数等于 SQLite 源表行数

#### Scenario: 目标表缺失时报错

- **WHEN** SQLite 源存在而 PostgreSQL 目标缺少某张应导入的表
- **THEN** 迁移工具报告缺失并终止，不静默跳过

### Requirement: 显式 tenant mapping 强制

迁移工具 SHALL 要求显式 tenant mapping；未提供 mapping 时 SHALL 拒绝导入并终止，不得静默把历史数据导入 `default` tenant。

#### Scenario: 缺少 mapping 拒绝导入

- **WHEN** 运行迁移而未提供 tenant mapping
- **THEN** 工具报错终止且不写入任何数据

#### Scenario: mapping 命中指定 tenant

- **WHEN** 提供 tenant mapping 后完成导入
- **THEN** 每条记录写入 mapping 指定的 tenant，且该 tenant 的记录对其它 tenant 不可见

### Requirement: 机器可读校验报告

校验工具 SHALL 输出机器可读（JSON）报告，至少覆盖逐表行数、关键字段 hash、引用完整性与语义抽样（向量 top-k、关键词搜索、session seq）；发现差异时 SHALL 以非零退出码报告，不得静默通过。

#### Scenario: 校验通过

- **WHEN** 源与目标在所有校验维度一致
- **THEN** 报告各维度全绿且退出码为 0

#### Scenario: 校验发现差异

- **WHEN** 某表行数或关键字段 hash 与源不一致
- **THEN** 报告明确指出差异表与差异维度且退出码非零

#### Scenario: 校验证据入库

- **WHEN** 一次校验完成
- **THEN** 机器可读结果保存为可复现的迁移证据文件

### Requirement: S0-S4 主数据源切换状态机

切换流程 SHALL 按 S0（SQLite primary）→ S1（snapshot import + shadow validation）→ S2（PostgreSQL primary + 审计窗口）→ S3（PostgreSQL stable）→ S4（旧实现清理）推进；每个状态 SHALL 明确 primary、允许写路径、对账方式与进入/退出条件；状态 SHALL 持久化，进程中断后可恢复推进。

#### Scenario: 满足退出条件后推进

- **WHEN** 当前状态的退出条件全部满足
- **THEN** 系统推进到下一状态并记录推进证据

#### Scenario: 切换中断后恢复

- **WHEN** 切换在 S1 与 S2 之间中断
- **THEN** 重启后从持久化状态继续，不重复执行已完成的状态步骤

### Requirement: primary 唯一性

任意时刻系统 SHALL 只有一个 primary 数据源；S2 起新写入 SHALL 以 PostgreSQL 成功为准，若保留 SQLite 仅作为审计副本，其写失败不得被当作主路径成功，也不得静默忽略。

#### Scenario: S2 写路径以 PostgreSQL 为准

- **WHEN** 系统处于 S2 且写入一条新消息
- **THEN** 持久化结果以 PostgreSQL 成功为准，SQLite 审计副本（若启用）的写失败被记录而不影响主路径结果

### Requirement: 回滚边界与演练

S0/S1 SHALL 可回滚到 SQLite primary；S2 之后在没有 PostgreSQL → SQLite 反向同步时 SHALL NOT 声明可无损回切 SQLite；staging SHALL 完成至少一次回滚演练并产出 runbook。

#### Scenario: S1 回滚演练

- **WHEN** 在 staging 处于 S1 并执行回滚
- **THEN** 系统回到 SQLite primary 且数据完整，回滚步骤记录为 runbook

#### Scenario: 禁止虚假回切承诺

- **WHEN** 系统处于 S2 且未实现 PostgreSQL → SQLite 反向同步
- **THEN** 迁移文档与工具不得声称可无损回切 SQLite

### Requirement: 对账报告

每次状态推进与 cutover SHALL 生成对账报告（至少逐表行数与主键集合），并保存为迁移证据。

#### Scenario: cutover 对账

- **WHEN** 系统从 S1 推进到 S2
- **THEN** 生成对账报告并保存为迁移证据

### Requirement: turn control plane 数据归属定案

turn control plane 持久化（turn 记录与查询日志，`turn_logs`）的归属已定案（ADR：`../../records/turn-audit-storage-ownership.md`）：纳入 PostgreSQL 侧，但**独立成 audit 库/schema（分库/分表）**，与 memory/session 主库隔离，不并入主库 schema；目标形态落地前允许 SQLite-only 过渡，过渡期 SHALL 记录一致性、备份/恢复与回滚策略；不得把「PostgreSQL 成为 primary」笼统解释为所有 session/turn 数据已迁移。

#### Scenario: 切换记录包含归属声明

- **WHEN** 系统进入 S2
- **THEN** 切换记录明确声明 turn 记录与查询日志的归属（独立 PG audit 库/schema；过渡期 SQLite-only 及其一致性/恢复策略）

### Requirement: PITR 恢复演练

staging SHALL 完成至少一次 PITR 恢复演练，恢复后的数据与指定时间点一致，并记录恢复步骤与 SLO（RPO ≤ 5 min、RTO ≤ 30 min）。

#### Scenario: PITR 恢复演练

- **WHEN** 按 runbook 执行 PITR 恢复到指定时间点
- **THEN** 恢复后的数据与该时间点一致，演练结果记录为证据
