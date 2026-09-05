# C12 observability/privacy/redaction + backup manifest 契约

> 对应任务计划：`openspec/openspec-tasks-bundle/task-12-observability-backup.md`（PILOT_ROADMAP §5.9.10 第 12 项）。
> 输入的已冻结决策（§5.9.17 / §5.9.12 / §7 / §10 DECIDED Persistence ownership、PROPOSED DEFAULT 日志与审计保留）不在此重复论证，design.md 逐条引用。

## Why

C12 是贯穿型 change（§5.9.10：「12 可以先定义契约并伴随各 change 落地」）。后续每个 Cxx change（durable control plane、admission、attachment、schedule 等）落地时都要同步实现自己的 §7.1 指标字段、redaction 与 backup manifest 条目；如果不在编码前把「什么允许采集、什么默认关闭、metrics label 允许什么、备份清单长什么样、legacy SQLite 如何隔离」冻结为可机检的契约，各 change 会各自发明口径，事后无法收敛。当前已有的 C0 观测底座（`core/telemetry/`：metrics registry、JSON/Prometheus 导出、trace store）只解决了「能采集」，没有解决「默认采什么、绝不采什么」与「备份覆盖什么」的契约问题。

本 change 交付 P-1 阶段的契约定义与可执行契约原语（redaction、label 白名单、retention、manifest 校验、事件 schema fixture），不实现消费这些契约的业务功能（总控台聚合 API、恢复演练、基线报告按伴随落地协议归 P0/P3）。

## What Changes

- **P-1 设计冻结**：采集规范与事件 schema（§7.1 字段表/派生耗时/聚合维度）、default content-off 与 debug 采集开关边界、redaction 分类规则、metrics label 白名单、retention 三档默认值（P-1 接受 §10 PROPOSED DEFAULT）、backup manifest schema 与校验规则（见 `design.md`，ADR 编号 ADR-1..ADR-8）。
- **契约代码**（全部为新增独立模块，不改既有运行路径）：
  - `core/telemetry/redaction.py` — secret/credential/PII/本地路径四类脱敏 + content capture gate（默认关闭、admin-only、短 TTL、开启即产生审计事件、入库前仍强制脱敏）；
  - `core/telemetry/label_policy.py` — metrics label 白名单 + 注册期校验（无 account/message/tool-call 高基数字段、无原始 tool args、无内容）；
  - `core/telemetry/retention.py` — operational 30d / audit 180d / 内容型 debug 7d 三档可配置 retention + 幂等文件清理 sweep；
  - `core/backup/manifest.py` — backup manifest 声明式 schema、模板加载与校验器（canonical store 全覆盖、`/tmp` 排除、legacy SQLite 独立 legacy-only 条目、secrets 独立归档、恢复顺序覆盖全部条目）。
- **契约 fixture**：
  - `tests/fixtures/observability_event_schema.json` — §7.1 生命周期事件字段、派生耗时公式、聚合维度、禁止内容字段、admin 访问审计事件 schema（单一来源，供各 Cxx 伴随落地对齐）；
  - `tests/fixtures/metric_label_policy.json` — label 白名单/禁止集契约（与代码白名单交叉校验）；
  - `tests/fixtures/backup_manifest_template.json` — Pilot backup manifest 模板（canonical store 声明现状），供校验器与后续 change 消费。
- **测试**：redaction 负向测试（content 默认关闭、脱敏命中、fail-safe）、label 规则测试（白名单外拒绝、高基数 id 拒绝）、retention job 测试（过期删除/未过期保留/幂等/dry-run）、manifest 校验测试（缺条目拒绝、`/tmp` 拒绝、legacy 非 legacy-only 拒绝、恢复顺序不全拒绝）、事件 schema 契约测试（fixture 与 §7.1 冻结字段一致、内容字段不在 label 白名单）。
- **证据**：`openspec/evidence/c12-observability-backup/` — pytest 输出 + pyright 结果。

## Capabilities

### New Capabilities

- `observability-privacy`：默认只采集结构化 lifecycle metadata；raw provider payload、完整 prompt、message content、tool args/result、attachment content 默认关闭；debug 采集须 admin 短期审计化开关且入库前脱敏；metrics label 白名单；retention 三档可配置且清理生效；admin 内容查看/下钻/导出产生审计事件；SLO 红线在基线报告前不进入代码。
- `backup-manifest`：backup manifest 显式列出 PostgreSQL、tenant workspace/blob root、配置与 secret 恢复方式，含一致性点、加密、校验与恢复顺序；legacy SQLite/workspace 为独立 legacy-only 条目；`/tmp` 不属于 durable backup；新增 canonical store 的 change 须同步登记 manifest 条目。

### Modified Capabilities

- 无（既有 `observability-load`（C0）的指标注册/导出/trace 契约不改；本 change 只在其上叠加默认采集边界与 label 白名单）。

## Non-Goals（明确不做）

- **不实现总控台聚合 API 与 Dashboard 展示**：聚合消费 work/turn/tool/delivery id 字段，须伴随 C2/C3 落地（task-12 共享 seam 协议）；本 change 只冻结聚合维度契约（fixture + spec）。
- **不实现恢复演练与基线报告**：属 P3 演练 / P0 稳定运行后产出（task-12 验收第 5、6 条），本 change 不产出。
- **不做 PG 行级 retention**：audit/operational metadata 的 PG 存储由 C2 durable control plane 建表；本 change 的 retention sweep 先覆盖文件型观测产物，PG 行级清理伴随表结构落地。
- **不接线 config.toml**：retention/debug 开关的 TOML 配置面在伴随落地时接线；本 change 以模块内配置对象 + Pilot 默认值冻结。
- **不实现 admin 内容查看端点**：本 change 只交付审计事件 schema 与 emit 原语；端点属 C5 auth/admin 边界。
- **不改 C0 既有指标/trace 行为**：`core/telemetry/metrics.py`、`metrics_export.py`、`trace_store.py` 保持现状。
- **不改旧单体运行时**：`session/`、`bus/`、现有日志路径不动。
- **不冻结任何 SLO 数字**：P95、失败率、RPO/RTO 红线等按 §10 DEFERRED BY EVIDENCE，等基线报告（负向测试防止 SLO 阈值混入实现）。

## Impact

- **代码**：新增 `core/telemetry/redaction.py`、`core/telemetry/label_policy.py`、`core/telemetry/retention.py`、`core/backup/__init__.py`、`core/backup/manifest.py`；无既有文件修改。
- **配置**：无新 TOML 配置项（模块级 dataclass 默认值；接线归伴随落地）。
- **依赖**：无新增第三方依赖（纯标准库）。
- **测试**：新增 `tests/observability_privacy/`（redaction/label/retention/事件契约，无 PG 依赖）+ `tests/backup_manifest/`（manifest 校验）。
- **系统/运维**：manifest 模板即 Pilot 备份清单的冻结起点；每个后续 Cxx change 落地 canonical store 时按「伴随落地协议」登记条目并通过校验器。
