# C6 附件生命周期加固 — design

> 对应 `openspec/changes/c6-attachment-hardening/`；扩展 `attachment-media` 规格，实现约束见下文 ADR，
> 偏离需先改本 design。上游事实来源：`openspec/specs/attachment-media/spec.md`（C6 已 sync）。
> 本 change 源于 C6 质量复盘，不改对外 HTTP 契约，只把实现拉回规格所述边界。

## 现状事实（复盘实测，2026-10-02）

| 事实 | 位置 | 后果 |
| --- | --- | --- |
| 上传只写 `staged` metadata，`commit_attachment` 生产零调用 | `bootstrap/attachments/service.py` | 附件长期停在 staged（本身符合 §5.9.15） |
| 对账已知集合取自 `list_committed_by_tenant`（只含 committed/missing） | `attachment_repo.py` | staged 附件被判 orphan → 删除自己的在库数据 |
| blob root = `workspace/attachments` 全局扁平，`storage_key` 无租户层级 | `chat_api.py:113`、`app.py:839` | 逐租户对账互删；实测 3 租户 blob 全灭 |
| 规格要求租户命名空间且「每个附件落在该 tenant 目录内」 | `specs/attachment-media/spec.md:80-90` | 实现与已 sync 规格相反 |
| `cleanup_orphans` 无年龄判据 | `blob_store.py` | rename→metadata 提交窗口可被误删 |
| Pillow 只捕 `UnidentifiedImageError` | `validation.py` | 截断/畸形图片抛裸 `OSError` → HTTP 500 |
| `max_decode_bytes` 声明+加载+测试断言，但从不参与判定 | `config_models.py:277` | 规格「解码内存 ≤64 MiB」未生效 |
| `AttachmentLifecycle.reconcile` 与 `runtime.reconcile_now` 两套实现 | `lifecycle.py`/`runtime.py` | 线上跑的不是被 `test_lifecycle` 覆盖的那套 |
| 周期轮租户集合取构造参数，生产未传 → 空 | `app.py:836-845` | 24h staging 清理与孤儿收敛只在启动发生 |
| `cleanup_expired` 先删 blob 再删行；`deleted_blobs += 0` | `lifecycle.py:59-66` | 行可存活而 blob 已无；报告恒报 0 个 blob |
| `delete.finished` 无生产调用方 | `service.emit` | §7.1 四类记录点缺一 |
| `secrets/auth_pepper` 被 `146736d2` 提交 | 仓库 | 密钥入库（本地未推送） |

## ADR-1 上传终态保持 `staged`，不在上传时推进 `committed`

**结论**：`upload` 成功后 metadata 停留 `staged`（deadline = 上传时刻 + `temp_ttl_hours`）；
「已知集合」必须把 `staged` 算作已知，从而不被对账删除。`commit_attachment` 保留为引用接入时
（协议帧带附件字段）的推进入口。

**理由**：§5.9.15 与 P-1 冻结值是「未引用 24h 清理 / 已引用自最后引用起 30d」。若为绕开误删而
在上传时就 commit，未引用附件会得到 30d 保留期，直接违反用户已冻结的参数。缺陷在 known 集合的
过滤条件，不在状态机。

**备选（不选）**：① 上传即 `commit_attachment`——保留期语义错；② 上传写 `committed` 且 deadline
用 `temp_ttl`——把 status 的含义用成两件事，`missing` 判定与引用推进都要再加分支；③ 给 staged
附件豁免对账——豁免逻辑仍要靠 status 猜，等于把同一Bug换个位置。

## ADR-2 `AttachmentBlobStore` 接收 workspace 根，每个方法以 `tenant_id` 为首参

**结论**：`AttachmentBlobStore(workspace_root, *, multi_tenant=True)`；
`tenant_root(tenant_id)` 派生 `tenants/<tenant_dirname>/attachments`（单机模式 `attachments/`），
`stage_bytes/commit/resolve_blob/read_bytes/delete_blob/blob_exists/list_blobs/find_orphan_keys/
cleanup_staging` 全部要求 `tenant_id`。`tenant_id` 缺失即 `AttachmentStorageError`（fail-closed）。
目录名沿用 C7 `TenantPathResolver` 的 `_tenant_dirname`，提为公共 `tenant_dirname` 作为单一来源。

**理由**：把租户作用域做进**类型签名**，忘记传 tenant 就无法调用；若只在 `storage_key` 里加租户
前缀，路径拼接仍可由调用方写错，而本次事故正是「调用方以为 root 是分租户的」。blob 必须与 C7
工具读取的 `attachments_root` 同一棵树，否则工具永远读不到 HTTP 上传的附件（C6 design ADR-3 已
澄清过这一点）。

**备选（不选）**：① 保留全局扁平根、`cleanup_orphans` 按 metadata 全表判定——已知集合一旦跨租户
就退化成「谁最后对账谁赢」，且 blob 无租户隔离面；② 在 blob_store 里构造 `ToolExecutionContext`
以复用 `attachments_root(context)`——为拿一个路径而伪造 8 个字段的工具上下文，是噪音。

## ADR-3 孤儿判定 = 本租户根下文件 ∖ 全部 metadata 键，且受 `orphan_grace_seconds` 保护

**结论**：`find_orphan_keys(tenant_id, known_keys, grace_seconds)`；mtime 在宽限期内的文件不判
孤儿（默认 900s，可配置为 0 关闭，测试用 0）。`missing` 判定仍只覆盖 `committed/missing` 行。

**理由**：两段式流程必然存在「rename 成功、metadata 未提交」的窗口（ADR-1 的状态机使这个窗口
合法），没有年龄判据时，对账与上传并发就会删掉正在进行的上传。宽限期把「崩溃残留」与「在途」
区分开，同时不削弱孤儿最终收敛。

**备选（不选）**：metadata 先行写入再 rename——会把「行存在但 blob 永不到来」变成常态，
`missing` 面比 orphan 面更难收敛，且 rename 失败要回滚行。

## ADR-4 对账只留一个实现，周期轮默认覆盖全部有附件记录的租户

**结论**：`AttachmentLifecycle.reconcile(tenant_id, *, dry_run=False)` 是唯一实现，返回
`LifecycleReport`（含 `dry_run`/`errors`/`to_dict()`，对齐 `core/telemetry/retention.SweepReport`
形态）；`runtime` 只负责节奏（`cleanup_interval_s` 为 tick，`reconcile_interval_s` 按比例触发）、
异常隔离与租户枚举。未显式配置 `tenant_ids` 时**每轮**都取 `repo.list_tenant_ids()`。
`ReconcileResult` 与 `runtime` 内的第二套对账逻辑删除。

**理由**：两套实现里线上那套没被 `test_lifecycle` 覆盖，是缺陷长期隐形的直接原因。生产默认
`tenant_ids=()` 使周期轮成为空操作，24h staging 清理只在重启时发生，与 §5.9.15 不符。

**备选（不选）**：只让 tick=0 全量、周期轮按配置子集（C6 原方案）——等价于「不重启就不清理」。

## ADR-5 清理顺序：先删 metadata 行，再删 blob

**结论**：`cleanup_expired` 先 `delete_attachment`（行不在则视为并发已处理，幂等跳过），
成功后再 `delete_blob`；`deleted_metadata`/`deleted_blobs` 分别真实计数。

**理由**：两种失败方向不对称——行删了而 blob 没删，留下 orphan，可被 ADR-3 的对账收敛；
blob 先删而行没删，则留下一个 `committed` 却无字节的行，用户视角是永久 404。C6 选了坏的方向，
且计数恒 0 使报告看不出删了什么。

## ADR-6 校验异常面归一到冻结错误码，不新增码

**结论**：`_probe` 捕获 `UnidentifiedImageError/OSError/ValueError/SyntaxError` →
`upload_type_denied`；`DecompressionBombError` → `upload_pixel_limit`；解码内存超限
（`pixels*4 > max_decode_bytes`）复用 `upload_pixel_limit`，message 区分。内部格式不符也直接
抛 `AttachmentError`，不再让裸 `ValueError` 逃到 HTTP 层。

**理由**：规格「单文件与资源硬上限」要求畸形图片不得拖垮进程且返回**冻结**错误码；500 会让客户端
无法区分「我的文件坏了」与「服务坏了」。新增 `upload_decode_memory_limit` 会扩冻结集，收益不及
保持一致性。

**备选（不选）**：给 `media_type` 层加 try/except 兜住一切再转 415——把分类逻辑推到 HTTP 层，
`error_type` 观测面会失去可比性。

## Risks / Trade-offs

- **物理路径变更**：blob 从 `workspace/attachments/*` 移到 `workspace/tenants/<dirname>/attachments/*`。
  main 未推送、生产未启用附件，无存量需搬迁；若某环境已试跑过，旧扁平目录文件将不再被读取，
  需要人工处置（对账会作为孤儿清理——作用域已不含旧根，故不会自动删，需运维确认后手动清）。
- **每轮全租户对账**：5000 租户规模下 = 每 `reconcile_interval`（默认 6h）一次全租户目录扫描。
  pilot 量级无压力；正式启用前应在 §5.9.15 演练里量化，必要时改为分片或增量水位。
- **解码内存估算按 RGBA 4 字节/像素**：对灰度/调色板图片偏保守（实际更小），可能拒掉个别
  合法大图；换取「一个可解释的硬上限」，且总像素上限本身已先行约束。
- **宽限期 900s**：真孤儿最长滞留 15 分钟 + 一轮对账；不做即误删在途上传，取舍明显。

## Rollback

单 commit 链可整体 revert：`fix(c6)` 各提交只触碰 `bootstrap/attachments/*`、
`attachment_repo.py`、`chat_api.py`、`app.py`、`path_resolver.py`、配置与测试，无 DB 迁移，
revert 即回到 C6 原行为（含其缺陷）。密钥 commit（`fix(security)`）应独立保留，不随 revert 撤销。

## 测试策略

- **回归守卫（必须失败于旧实现）**：`test_reconcile_keeps_uploaded_staged_attachment`、
  `test_reconcile_does_not_touch_other_tenant`、`test_blob_store.py::test_blob_root_is_tenant_namespace`
  /`test_tenants_are_physically_disjoint`/`test_orphan_grace_spares_in_flight_upload`、
  `test_runtime.py::test_uploaded_attachment_survives_roundtrip`（Service.upload → 两轮对账 →
  字节一致）、`test_cleanup_emits_delete_events`、`test_cleanup_idempotent_and_skips_referenced`
  （断言 `deleted_blobs == 1`）、`test_truncated_image_rejected_not_crash`（415 而非 500）、
  `test_decode_memory_budget_rejected`。
- **契约层**：`test_http_api.py` 保留 attachment_id 契约 + 旧 `?path=` 400 + 跨租户 404 + dev 503，
  并把 dev 断言扩到「不产生 `tenants/` 目录」。
- **静态**：改动文件 `--level error` 0 errors；project 全局错误数与 C6 基线（38）对照不新增。
- **全量**：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/` 与 C6 的 1835 基线对照。
