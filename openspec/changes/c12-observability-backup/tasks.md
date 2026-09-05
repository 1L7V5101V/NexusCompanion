# C12 observability/privacy + backup manifest — 实施任务

> 对应 `openspec/openspec-tasks-bundle/task-12-observability-backup.md`；证据统一落 `openspec/evidence/c12-observability-backup/`。
> 管理闭环：任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新 task-12 / PILOT_ROADMAP_PROJECT_CHECKLIST 状态。
> 贯穿型 change：本 change 交付 P-1 契约层；§4「伴随落地」条目在后续 Cxx change 内执行，不随本 change 勾选。

## 1. change 文档

- [x] 1.1 proposal/design/specs/tasks 四件套落盘（`specs/observability-privacy`、`specs/backup-manifest`）。验证：`openspec validate` 通过；ADR-1..ADR-8 覆盖 task-12「风险与需冻结决策」全部条目

## 2. redaction 与 content gate（ADR-1/2/3）

- [x] 2.1 `core/telemetry/redaction.py`：`RedactionCategory`（secret/credential_url/local_path/pii）、`redact_text`（类型化占位符）、`redact_value`（递归、深度/条数上限、异常 fail-safe）、丢弃/命中计数。验证：单元测试覆盖四类命中与 fail-safe
- [x] 2.2 `ContentCaptureGate`：默认关闭、`open(principal, reason, ttl)` 三要素齐备才开启、TTL 惰性过期、开启即返回审计事件、重启即回关闭态。验证：负向测试（缺要素拒绝、TTL 过期拒绝、重启回默认）
- [x] 2.3 测试：`tests/observability_privacy/test_redaction.py`、`test_content_gate.py`。验证：pytest 通过

## 3. metrics label 白名单（ADR-4）

- [x] 3.1 `core/telemetry/label_policy.py`：allowlist（含 C0 既有 `channel`/`backend`）、禁止集（高基数 id + 内容字段）、`PolicyCheckedMetricRegistry` 注册期校验。验证：白名单外注册抛错
- [x] 3.2 fixture `tests/fixtures/metric_label_policy.json` + 契约测试（代码白名单与 fixture 一致；身份字段不在白名单）。验证：`tests/observability_privacy/test_label_policy.py` 通过

## 4. 事件 schema fixture（ADR-7）

- [x] 4.1 `tests/fixtures/observability_event_schema.json`：§7.1 七类字段表、8 项派生耗时、聚合维度、禁止内容字段、admin 访问审计事件 schema。验证：契约测试比对 §7.1 冻结字段
- [x] 4.2 契约测试：fixture 字段 ↔ §7.1 一致；事件身份字段 ∩ label 白名单 = ∅。验证：`tests/observability_privacy/test_event_schema_contract.py` 通过

## 5. retention（ADR-5）

- [x] 5.1 `core/telemetry/retention.py`：`RetentionPolicy`（30/180/7 可配置）、`sweep_roots()`（mtime 过期删除、幂等、dry-run、目录缺失降级、逐条错误记录）。验证：单元测试
- [x] 5.2 测试：过期删除/未过期保留、幂等重跑零删除、dry-run 零副作用。验证：`tests/observability_privacy/test_retention.py` 通过

## 6. backup manifest（ADR-6）

- [x] 6.1 `core/backup/manifest.py`：manifest schema dataclass、模板加载、校验器（kind 全集覆盖、一致性点/校验和必填、恢复顺序覆盖、`/tmp` 与 Windows 临时目录排除、secrets 独立、legacy legacy-only）。验证：正/负向测试
- [x] 6.2 fixture `tests/fixtures/backup_manifest_template.json`（Pilot canonical store 现状声明）。验证：模板本身通过校验器
- [x] 6.3 测试：`tests/backup_manifest/test_manifest.py`。验证：pytest 通过

## 7. 静态负向与验证（ADR-8）

- [x] 7.1 SLO 阈值静态负向测试：扫描 `core/telemetry/`、`core/backup/` 无 SLO 红线判定。验证：`tests/observability_privacy/test_no_slo_thresholds.py` 通过
- [x] 7.2 pyright 零新增错误（对照 main 基线 36 既有错误）；pytest `-W error` 全绿。验证：输出存 `openspec/evidence/c12-observability-backup/`

## 8. 伴随落地协议（后续 Cxx change 内执行，本 change 不勾选）

- [ ] 8.1 C2/C3 落地 work/turn/tool/delivery 表时，按 fixture 实现其 §7.1 指标字段与事件记录点（E10）
- [ ] 8.2 C5 落地 admin 端点时，内容查看/下钻/导出接入 `AdminAccessAuditEvent` emit
- [ ] 8.3 C6 落地 attachment blob root 时，细化 manifest `tenant_workspace` 条目并复跑校验器
- [ ] 8.4 P0：retention 接线 config.toml 与进程内定时执行；总控台聚合 API（消费各 Cxx 指标）
- [ ] 8.5 P0 后期：基线报告（backlog/latency/failure rate/cancellation/compensation/recovery window）→ 之后才可由后续 change 以配置引入 SLO 阈值
- [ ] 8.6 P3：恢复演练报告（manifest 恢复 + PITR + attachment orphan/missing reconciliation）
