## Why

C6（`c6-attachment`，已归档合入 main）落地后的质量复盘发现两个会**静默销毁用户数据**的缺陷，
以及若干契约未真正生效的收口项。两者都由「容器每次启动」触发，因此 main 当前不可部署：

1. **上传终态被对账判为孤儿删除**。`AttachmentService.upload` 在 blob rename 后只写 `staged`
   metadata（这是正确终态——§5.9.15 冻结「未引用 24h 清理」，若上传即 commit 会得到 30d 保留，
   反而违反冻结值），但 reconciliation 的「已知集合」取自 `list_committed_by_tenant`，只含
   `committed/missing`。于是每个刚上传成功的附件都在下一轮对账里被 `cleanup_orphans` 删除。
2. **blob root 是全局扁平目录，per-tenant 对账跨租户删除**。装配用 `workspace/attachments` 单层
   根、`storage_key` 不含租户层级，而规格要求租户命名空间（`attachment-media`「blob 落 tenant
   命名空间」，含「每个附件落在该 tenant 目录内」场景）。启动轮遍历全部租户逐个对账，每轮只保留
   本租户文件，实测三个租户的 blob 全部消失。

另有 `secrets/auth_pepper` 被 `146736d2` 误提交（本地未推送），以及 allowlist 之外的畸形图片
冒 500、`max_decode_bytes` 只声明不执行、对账有两套实现且线上跑的不是被覆盖的那套、
`delete.finished` 记录点无调用方、清理报告 blob 计数恒 0、生产周期对账实际空转、媒体响应
整体入内存等收口项。

## What Changes

- **blob 存储改为租户作用域**：`AttachmentBlobStore(workspace_root, multi_tenant=...)` 接收
  workspace 根，每个公开方法以 `tenant_id` 为第一参数派生 `tenants/<tenant_dirname>/attachments`
  根（与 C7 `TenantPathResolver` 同一棵树，目录名函数提为公共 `tenant_dirname` 作为单一来源）。
  跨租户在同一物理层面无可见性，`orphan/staging` 扫描作用域天然等于单租户。
- **对账已知集合覆盖全部状态**：新增 `list_by_tenant`（含 `staged`）作为 known 来源，
  `missing` 判定仍只看 `committed/missing`。删除 `list_committed_by_tenant`。
- **在途写入保护**：新增 `orphan_grace_seconds`（默认 900s），rename 完成但 metadata 未提交的
  blob 在宽限期内不判孤儿。
- **对账实现合一**：`AttachmentLifecycle.reconcile(tenant_id, dry_run=...)` 成为唯一实现并支持
  dry-run；`AttachmentLifecycleRuntime.reconcile_now` 只委托与控节奏；周期轮在未显式配置
  `tenant_ids` 时覆盖全部有附件记录的租户（生产默认此前是空转）。
- **清理顺序与计数**：`cleanup_expired` 先删 metadata 行再删 blob（blob 删除失败只留下可被对账
  收敛的孤儿，绝不留下指向已删 blob 的行）；`deleted_blobs` 真实计数；每个删除发
  `delete.finished`，使 §7.1 四类记录点全部在生产路径可达。
- **校验异常面收口**：Pillow 的 `UnidentifiedImageError/OSError/ValueError/SyntaxError/
  DecompressionBombError` 全部归一到冻结错误码（畸形/截断图片 → `upload_type_denied`，
  超大像素 → `upload_pixel_limit`），不再冒 500；`max_decode_bytes` 落地为真实闸门
  （按 RGBA 4 字节/像素估算）；删除未使用的 `_pixel_budget_over`/`ExtensionMismatch`/`logger`。
- **HTTP 层**：`Content-Length` 预检超限（ADR-7 要求，避免为注定失败的上传读满 body）；
  `GET /api/chat/media` 改 `FileResponse` 流式回传（此前 20 MiB 整体入内存）并清理死变量。
- **仓储**：`list_expired` 的到期条件与 `limit` 下推 SQL（此前每小时全表取回后在应用层过滤）。
- **密钥遏制**：`secrets/auth_pepper` 取消跟踪并补 `.gitignore` 规则（`/secrets/`、
  `**/auth_pepper`）。
- **文档**：`config.example.toml` 补 `[agent.attachments]` 冻结默认与注释。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `attachment-media`：ADDED「reconciliation 作用域限定与在途写入保护」——把此前只靠实现隐含、
  因而被写错的边界写成可验收契约（已知集合含 staged、按租户根扫描、宽限期、dry-run 零变更）。

## Impact

- 代码：`bootstrap/attachments/{blob_store,service,lifecycle,runtime,validation}.py`、
  `bootstrap/db/repository/attachment_repo.py`、`bootstrap/chat_api.py`、`bootstrap/app.py`、
  `agent/tools/path_resolver.py`（`_tenant_dirname` → `tenant_dirname`）、
  `agent/config{,_models}.py`、`config.example.toml`、`.gitignore`。
- 测试：`tests/attachments/`（新增跨租户互不侵犯、上传→对账→取回端到端、dry-run、宽限期、
  畸形图片 415、解码内存上限、delete 事件可达等回归守卫）。
- 数据/迁移：无 schema 变更，无新迁移。`storage_key` 语义不变（相对租户 blob 根），
  物理路径变化只影响尚未部署过的环境；main 未推送、生产未启用附件，无在库数据需搬迁。
- 备份：blob root 仍在 `tenant_workspace` 条目覆盖的 workspace 之下，manifest 的
  `tenant-workspace` 一致性点描述无需改写。
- BREAKING：无新增对外契约变化（`uploads`/`media` 的 attachment_id 契约沿用 C6）。

## Non-Goals

- 不在本协议帧里引入附件字段（沿用 C6 Non-Goal）；`commit_attachment`/`add_reference`/
  `remove_reference` 仍无生产调用方，30d「已引用」保留期在引用接入后才可验收。
- 不新增 manifest kind，不改校验器。
- 不做服务端转码/缩略图，不加 `attachment` 渲染前端。
- 不引入 per-tenant 存储总量配额（规格未要求，属容量治理，另议）。
- 不改事件字段集与 metrics label 白名单（沿用单一来源 fixture）。
