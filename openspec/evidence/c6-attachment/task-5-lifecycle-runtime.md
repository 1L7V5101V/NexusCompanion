# Task 5 evidence — 清理与 reconciliation 接线

- 实现：`bootstrap/attachments/runtime.py`（AttachmentLifecycleRuntime）、
  `bootstrap/attachments/lifecycle.py`（repo/blob_store 公开属性）、
  `bootstrap/db/repository/attachment_repo.py`（list_tenant_ids）、
  `bootstrap/app.py`（`_start_attachment_lifecycle`/`_stop_attachment_lifecycle`
  + shutdown 序列 entry）、`agent/config_models.py`/`agent/config.py`
  （cleanup_interval_s/reconcile_interval_s/reconcile_enabled/reconcile_on_startup）。
- 测试：`tests/attachments/test_runtime.py`（5）。

## 回归输出

```
uv run pytest tests/attachments/ tests/test_chat_api.py
  tests/test_plugin_config_schema.py tests/test_default_memory_plugin_config.py
  → 80 passed
uv run pyright --level error bootstrap/app.py bootstrap/attachments/*.py
  agent/config.py agent/config_models.py
  → 0 errors
```

## 设计确认（ADR-10 / SweepReport 契约）

- `AttachmentLifecycleRuntime.run()` 周期循环：每 tick cleanup_expired + 按
  interval 触发 reconcile；tick=0 首轮按 `reconcile_on_startup` 对**全部有附件
  记录的租户**（repo.list_tenant_ids）启动对账一次（best-effort，异常不阻断启动）；
- 后续周期轮按配置 tenant_ids（prod 通常为空 → 周期轮只做 cleanup，全租户
  对账由启动 + 手动触发承担，符合「非每日 SLA」冻结语义）；
- `reconcile_now(tenant_id, dry_run=…)`：手动/演练入口，报告形态复用
  `core/telemetry/retention.SweepReport` 契约（dry_run/to_dict 语义）；
  dry_run 只统计 missing/orphan/staging 将处理数，不删除、不改 metadata
  （负向断言：dry_run 后 committed 仍 committed、孤儿 blob 仍在磁盘）；
- 非 dry_run 路径：删除孤儿 → 清理超龄 staging → 逐个标 missing（异常逐条记
  errors 不阻断）；
- app.py 装配：仅当 webchat_durable（PG durable）+ attachments.enabled 时启动；
  `[agent.attachments].reconcile_on_startup` 控制启动对账；shutdown 挂
  `attachments_lifecycle.stop`（webchat_durable.close 后、runtime_tasks.cancel 前）；
- 配置：`cleanup_interval_s`/`reconcile_interval_s` 允许 0（禁用周期任务，
  仅启动对账 + 手动），`_nonneg_int` 校验非负。

## 测试要点

- dry_run 报告不落盘：committed 未变 missing、孤儿 blob 仍在 → 真 PG 断言；
- 实删路径：孤儿删除 + missing 标记；
- committed 有 blob 不算 orphan（有 metadata）；
- 周期 harness：`_run_round(0)` 在 cleanup 抛错时不阻断 reconcile（`report=None`
  降级继续）。

## Task 5.2 — backup manifest 细化（C12 §8.3）

- `tests/fixtures/backup_manifest_template.json` `tenant-workspace` 条目已冻结：
  consistency_point 「PG 恢复点对齐（attachment blob root = workspace/attachments/
  租户命名空间，与 PG metadata 同一次点恢复）」；retention 「已引用附件 30d /
  临时 staging 24h（C6 冻结）」；checksum 「逐文件 sha256（含 attachment blob）」；
  notes 「含 attachment blob root…；staging（.staging/）与 /tmp 为临时区不入
  backup；orphan/missing 由 reconciliation 处置」。
- `tests/backup_manifest/` 校验器 14 passed（未新增 kind，沿用 tenant_workspace）；
- C12 `c12-observability-backup/tasks.md` §8.3 已勾选并注明 owner C6 完成。