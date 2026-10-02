# C6 附件生命周期加固 — 实施任务

> 对应 `openspec/changes/c6-attachment-hardening/`；design.md ADR-1..ADR-6 为实现约束，偏离需先改 design。
> 分支：`fix/c6-attachment-hardening`（主工作区 `D:/.Projects/NexusCompanion`）。
> 证据统一落 `openspec/evidence/c6-attachment-hardening/`。
> 背景：C6 复盘发现两处会静默销毁数据的缺陷（main 未推送前拦下），本 change 只做收口，
> 不改对外 HTTP 契约、无 DB 迁移。

## 1. 密钥遏制（独立于附件逻辑，先落）

- [x] 1.1 `git rm --cached secrets/auth_pepper` + `.gitignore` 增 `/secrets/`、`**/auth_pepper`；
  验证：`git ls-files secrets/` 为空、`git check-ignore -v secrets/auth_pepper` 命中、
  本地 pepper 文件未被删除（运行期会话 digest 不变）。证据：`task-1-secret-containment.md`。

## 2. blob root 落租户命名空间（ADR-2）

- [x] 2.1 `agent/tools/path_resolver.py`：`_tenant_dirname` → 公共 `tenant_dirname`（单一来源），
  两处内部引用同步。验证：`grep -rn "_tenant_dirname"` 无残留；C7 路径测试通过。
- [x] 2.2 重写 `bootstrap/attachments/blob_store.py`：构造接收 workspace 根 + `multi_tenant`，
  `tenant_root(tenant_id)` 派生 `tenants/<dirname>/attachments`（单机回退 `attachments/`），
  全部方法以 `tenant_id` 为首参；`tenant_id` 为空即 `AttachmentStorageError`（fail-closed）。
  验证：`tests/attachments/test_blob_store.py`（含 `test_blob_root_is_tenant_namespace`、
  `test_tenants_are_physically_disjoint`、`test_missing_tenant_id_fails_closed`）。
- [x] 2.3 装配同步：`chat_api.py` 与 `app.py` 改传 workspace 根 + `multi_tenant=True`。
  验证：dev 模式（无 auth/durable）不产生 `tenants/` 目录（`test_http_api` 断言扩展）。

## 3. 对账已知集合与在途保护（ADR-1/ADR-3）

- [x] 3.1 `attachment_repo.py`：新增 `list_by_tenant`（全部状态）取代 `list_committed_by_tenant`。
  验证：`test_list_by_tenant_covers_all_statuses` 断言 staged 与 committed 均枚举到。
- [x] 3.2 `AttachmentConfig` 新增 `orphan_grace_seconds`（默认 900）+ `_load_attachment_config`
  走 `_nonneg_int`；`find_orphan_keys/cleanup_orphans` 按 mtime 宽限判定。
  验证：`test_orphan_grace_spares_in_flight_upload`、`test_attachment_config_frozen_defaults`。
- [x] 3.3 `config.example.toml` 补 `[agent.attachments]` 注释块（冻结默认 + allowlist 不可配置）。
  验证：TOML 解析通过（`load_config` 用例覆盖）。

## 4. 对账实现合一与周期节奏（ADR-4）

- [x] 4.1 `lifecycle.py`：`reconcile(tenant_id, *, dry_run=False)` 为唯一实现，返回
  `LifecycleReport`（`dry_run`/`errors`/`to_dict()` 对齐 `SweepReport` 形态）；注入 `config`。
  验证：`test_reconcile_dry_run_reports_without_mutating`、`test_reconcile_keeps_uploaded_staged_attachment`、
  `test_reconcile_does_not_touch_other_tenant`。
- [x] 4.2 `runtime.py`：删除第二套对账逻辑与 `ReconcileResult`；`reconcile_now` 委托 lifecycle；
  `_tenant_ids_for_round` 在未显式配置时每轮取 `list_tenant_ids()`。
  验证：`test_periodic_round_covers_all_tenants_when_unset`（tick>0 覆盖两个租户）、
  `test_run_first_round_reconciles_and_loop_harness`（cleanup 抛错仍对账）。
- [x] 4.3 `LifecycleReport.changed` 供日志判据，避免每轮空报告刷屏。验证：runtime 用例通过。

## 5. 清理顺序、计数与记录点（ADR-5）

- [x] 5.1 `cleanup_expired` 先删 metadata 行、成功后删 blob；行已被并发删除则幂等跳过；
  `deleted_metadata`/`deleted_blobs` 真实计数。
  验证：`test_cleanup_idempotent_and_skips_referenced` 断言 `deleted_blobs == 1` 且第二轮全 0。
- [x] 5.2 每个到期删除发 `delete.finished`，`cleanup.finished` 保留汇总；删除 `service.emit`
  死分发器（`delete_finished` 由此获得唯一生产调用方）。
  验证：`test_cleanup_emits_delete_events`（spy telemetry 断言 1 条 delete + 1 条 cleanup）。

## 6. 校验异常面与资源上限（ADR-6）

- [x] 6.1 `_probe` 捕获 Pillow 全异常面归一到冻结码；格式不符抛 `AttachmentError`；
  `DecompressionBombError` → `upload_pixel_limit`。
  验证：`test_truncated_image_rejected_not_crash`（png/jpg/gif/webp 截断）、
  `test_http_api.py::test_upload_truncated_image_rejected_415`（HTTP 层 415 而非 500）。
- [x] 6.2 `max_decode_bytes` 落地：`pixels*4 > max_decode_bytes` → `upload_pixel_limit`。
  验证：`test_decode_memory_budget_rejected`。
- [x] 6.3 删除死代码：`_pixel_budget_over`、`ExtensionMismatch`（含 `__all__`）、
  validation 未使用的 `logger`/`logging`/`field` 导入。验证：pyright 0 errors。

## 7. HTTP 与仓储收口

- [x] 7.1 `POST /api/chat/uploads` 读 body 前按 `Content-Length` 预检 413；非法头 400。
  验证：`test_upload_content_length_preflight` 仍 413 `upload_too_large`。
- [x] 7.2 `GET /api/chat/media` 改 `FileResponse` 流式（`service.fetch` 返回 `(record, Path)`），
  删除 `_ = filename` 死变量与 20 MiB 全量入内存路径；Content-Disposition 用回退名。
  验证：`test_upload_then_media_roundtrip` 字节一致；pyright 0 errors。
- [x] 7.3 `list_expired` 到期条件（`retention_deadline <= now` 且 staged 或无引用）与 `limit`
  下推 SQL + 按 deadline 排序。验证：`test_attachment_repo.py` 到期用例全绿。

## 8. 回归闸门与文档回填

- [x] 8.1 端到端守卫：`Service.upload` → 启动轮 + 周期轮 → blob 仍在 → `fetch` 字节一致。
  验证：`test_runtime.py::test_uploaded_attachment_survives_roundtrip`（旧实现必失败）。
- [x] 8.2 定向回归：`tests/attachments/ + tests/test_chat_api.py + tests/test_channel_base.py +
  tests/backup_manifest/`。证据：`task-8.2-targeted-and-pyright.txt`。
- [x] 8.3 全量：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/`。证据：`task-8.3-full-regression.txt`。
- [x] 8.4 pyright：改动文件 `--level error` 0 errors；project 全局错误数与 C6 基线对照不新增。
  证据：并入 `task-8.3-full-regression.txt` 的 pyright 段。
- [x] 8.5 `openspec validate c6-attachment-hardening` 通过。
- [x] 8.6 `PILOT_ROADMAP_PROJECT_CHECKLIST.md` 刷新：C6 行补「加固」证据链与当前阶段/next decision；
  `SCALING_ROADMAP` 状态同步。证据：本文件勾选 + checklist diff。
