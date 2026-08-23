# observability-load 增量规格

## Purpose

可观测性与负载工具：为 NexusCompanion 提供可重复的负载/基准工具、进程内指标注册与导出、以及按 turn_id 串起各处理阶段的追踪表面，支撑迁移基准（C1B）与并发验收（C1D）的可复现观测。

## ADDED Requirements

### Requirement: 可重复负载工具

负载工具 SHALL 以可配置的输入（租户数、通道、注入速率、总轮次）对选定的存储后端（SQLite 或 PostgreSQL）驱动 turn 级负载，并 SHALL 输出机器可读（JSON）的运行结果；对相同配置重复运行 SHALL 产出可复现、可比较的结果，且结果可保存为证据文件。

#### Scenario: 指定后端重复运行

- **WHEN** 操作者以相同参数分别对 SQLite 与 PostgreSQL 后端运行负载工具
- **THEN** 工具产出机器可读结果，包含每后端的成功/失败轮次与耗时统计，两次运行的口径一致、可复现

#### Scenario: 结果可复现

- **WHEN** 操作者以相同配置再次运行负载工具
- **THEN** 结果文件记录相同的输入参数与统计口径，运行间差异可由输入参数解释

#### Scenario: 证据入库

- **WHEN** 一次负载运行完成
- **THEN** 机器可读结果保存为证据文件，可被迁移基准或容量验收引用复现

### Requirement: 指标注册与导出

系统 SHALL 提供进程内指标注册（计数、直方图、秒表）并 SHALL 以机器可读格式（JSON 与 Prometheus 文本）导出指标；导出 SHALL 可被现有 dashboard 消费。

#### Scenario: 导出包含已注册指标

- **WHEN** 系统注册并更新若干指标后请求导出
- **THEN** 导出的 JSON 与 Prometheus 文本均包含这些指标的最新值

#### Scenario: 导出端点可达

- **WHEN** 服务运行时请求指标导出端点
- **THEN** 端点返回 200 与可解析的指标数据，无需额外认证依赖

### Requirement: turn_id 追踪表面

系统 SHALL 为每个 turn 维护按 turn_id 关联的处理阶段记录（进入/退出时间、耗时、结果），覆盖当前已存在的处理链路；同一 turn_id SHALL 在其生命周期事件与追踪记录中保持一致。

#### Scenario: 单 turn 生成追踪记录

- **WHEN** 一个 turn 完整走完各处理阶段
- **THEN** 存在一条按 turn_id 关联的记录，含各阶段时间与最终结果

#### Scenario: 后端切换后追踪连续

- **WHEN** 系统存储后端从 SQLite 切换到 PostgreSQL 后运行 turn
- **THEN** 该 turn 的追踪记录仍然完整生成，turn_id 可跨阶段关联
