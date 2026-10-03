# task-7.1 / 7.2 — 观测报告与隐私边界（ADR-6/ADR-7）

日期：2026-10-03

## 报告形态

- 每轮每实体一条 `EntityReport`：`category / target / scanned / deleted / kept /
  bytes_freed / dry_run / errors / recoverability`——前八字段集与
  `core.telemetry.retention.SweepReport.to_dict()` 一致（超集，加分类字段）。
- **可恢复性分类**（ADR-7，owner 要求"能回答这次删掉多少是永久丢失"）：
  - `recoverable`：`webchat_replay_frames`（派生补发缓冲，canonical 消息为真源）；
  - `irrecoverable`：三条审计流（tool_audit/admin_audit/work_attempts）、凭据摘要作废、
    观测分片文件（事实记录）。
  - `RetentionRunReport.recoverable_deleted / irrecoverable_deleted / *_bytes` 分类合计，
    汇总数 = 分类数之和（测试断言）。
- 错误串过 `core.telemetry.redaction.redact_text`（逐实体 try/except，单点失败不中断整轮）；
  实体日志只打类别/计数/体量/dry_run/分类/错误条数，不打被删对象内容。

## 测试证据

- `test_report_classifies_recoverable_vs_irrecoverable`：同轮裁剪补发缓冲 + 审计流水 →
  分类计数正确、汇总 = 分类之和（spec「报告区分可恢复与不可恢复的删除量」）；
- `test_report_free_text_errors_redacted`：注入含本地路径与密码值的错误 → 报告中
  `[REDACTED:local_path]`、无 `hunter2`、无原始路径（spec「报告不落敏感内容」）；
- `test_dry_run_reports_without_mutating_and_matches_live`：演练报告含
  `dry_run=True`、"将删除"计数与后续实删轮一致；
- 体量估算口径：PG 腿 = 被删行最大文本列 `octet_length`（tool_audit=`arguments_redacted`、
  admin_audit=`detail::text`、work_attempts=`error`、replay=`frame_json`）；文件腿 =
  文件 size；凭据作废 = 0（UPDATE 不释放行存储，计数进 deleted）。

## task 7.2 — 不新增 metrics label / 不扩事件白名单

```
grep -rn "validate_label_names" bootstrap/retention/ core/telemetry/retention.py  → 无调用点
grep -rn "ALLOWED_EVENT_FIELDS"   bootstrap/retention/                            → 无引用
grep -rn "Counter\|Histogram\|Gauge" bootstrap/retention/                          → 无新指标
```

本 change 未注册任何新 label/指标/事件字段；`tests/observability_privacy/` 既有白名单
与 fixture 契约测试在定向回归（`task-8.1-targeted.txt`，267 passed）中保持全绿。
