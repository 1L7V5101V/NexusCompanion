# backup-manifest 增量规格

## Purpose

定义 Pilot backup manifest 的声明式 schema 与校验契约：manifest SHALL 显式列出 PostgreSQL、tenant workspace/blob root、必要配置与 secret 恢复方式，并给出一致性点、加密、校验与恢复顺序；legacy SQLite/workspace SHALL 为独立 legacy-only 条目；`/tmp` SHALL NOT 属于 durable backup；新增 canonical store 的 change SHALL 同步登记 manifest 条目（伴随落地协议）。

## ADDED Requirements

### Requirement: manifest 覆盖 canonical store 全集

backup manifest SHALL 声明以下 kind 的备份条目：`postgresql`（canonical store）、`tenant_workspace`（含 attachment blob root 的 tenant 命名空间）、`config`、`secrets`；缺失任一 kind SHALL 校验失败。每条 entry SHALL 声明路径、加密方式、校验和、一致性点与保留策略。

#### Scenario: 缺少 canonical store 条目校验失败

- **WHEN** 对缺少 `secrets` 或 `tenant_workspace` 条目的 manifest 执行校验
- **THEN** 校验失败并报告缺失的 kind，manifest 不被接受为有效备份清单

#### Scenario: 完整 manifest 校验通过

- **WHEN** 对包含 postgresql、tenant_workspace、config、secrets 全部条目且字段齐全的 manifest 执行校验
- **THEN** 校验通过

### Requirement: 一致性点、加密与校验和为必填声明

manifest 的每个条目 SHALL 声明一致性点（备份一致的时刻或机制）与校验方式；缺任一声明 SHALL 校验失败，SHALL NOT 出现「无一致性点的备份条目」。

#### Scenario: 缺一致性点的条目被拒绝

- **WHEN** 某条目未声明一致性点或未声明校验和
- **THEN** 校验失败并指明缺失字段所在条目

### Requirement: 恢复顺序覆盖全部条目

manifest SHALL 提供恢复顺序声明，且恢复顺序 SHALL 恰好覆盖全部条目 id；顺序缺失或引用不存在条目 SHALL 校验失败。

#### Scenario: 恢复顺序遗漏条目被拒绝

- **WHEN** manifest 声明的恢复顺序未包含某已登记条目
- **THEN** 校验失败并指明未被排序覆盖的条目

#### Scenario: 恢复顺序引用未知条目被拒绝

- **WHEN** 恢复顺序中出现未登记的条目 id
- **THEN** 校验失败

### Requirement: 临时目录不属于 durable backup

manifest 中任何条目路径命中临时目录（POSIX `/tmp` 及等价 Windows 用户临时目录）SHALL 校验失败；`/tmp` 下的任何产物 SHALL NOT 出现在备份清单中。

#### Scenario: /tmp 路径条目被拒绝

- **WHEN** 某条目路径位于 `/tmp/` 或 Windows 用户临时目录下
- **THEN** 校验失败并指明该条目违反临时目录排除规则

### Requirement: secrets 独立于普通业务备份

secret 的备份 SHALL 为独立条目并单独声明加密与恢复方式；普通业务数据条目 SHALL NOT 声明包含明文 secret；违反 SHALL 校验失败。secret 来源、轮换与灾难恢复权限 SHALL 在条目元数据中说明，SHALL NOT 与普通业务备份混成明文归档。

#### Scenario: secret 混入业务条目被拒绝

- **WHEN** 某业务条目声明 `contains_secrets=true` 而未提供独立 secrets 条目
- **THEN** 校验失败

#### Scenario: 独立 secrets 条目通过

- **WHEN** secrets 为独立条目且声明了独立加密方式
- **THEN** 该条目校验通过

### Requirement: legacy SQLite/workspace 独立且只恢复到 legacy 模式

旧单体 SQLite 与其 workspace SHALL 作为独立备份条目登记（路径、校验和、保留策略独立声明），SHALL 声明 `restore_mode` 为仅 legacy 模式；SHALL NOT 声明恢复进 Pilot PostgreSQL，也 SHALL NOT 作为 Pilot 的 fallback 或双写目标。

#### Scenario: legacy 条目缺 legacy-only 声明被拒绝

- **WHEN** `legacy_sqlite` 条目未声明 `restore_mode="legacy_only"` 或未声明独立于 Pilot 条目
- **THEN** 校验失败

#### Scenario: legacy 条目合规

- **WHEN** legacy SQLite 条目独立登记且声明 restore_mode 为 legacy-only
- **THEN** 校验通过，且该条目与 Pilot PostgreSQL 条目在恢复顺序中互不依赖

### Requirement: 新增 canonical store 同步登记（伴随落地协议）

后续 change 引入新的 canonical store（如 durable control plane 表、attachment metadata、schedule 存储）时，SHALL 在同一 change 内更新 manifest 模板并通过校验器；manifest 模板 fixture SHALL 保持与已落地 canonical store 集合一致。

#### Scenario: 新 store 未登记即无法通过全量校验

- **WHEN** 依据模板校验规则核对「已声明 kind 集」与「已落地 canonical store 清单」
- **THEN** 每个已落地的 canonical store 在模板中都有对应条目，不存在「已落地但未登记」的缺口
