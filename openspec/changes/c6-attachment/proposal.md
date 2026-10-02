# C6 attachment/media — immutable attachment_id + tenant 隔离 + fail-closed 内容边界

> 对应 PILOT_ROADMAP §5.9.15（Attachment/media lifecycle）、§5.9.12（Persistence ownership 与 backup manifest）、
> §5.9.9（DB rollout/schema evolution）、§7.1（必采集数据）、§10 PROPOSED DEFAULT（Attachment policy，本 change 正式冻结）。
> 上游依赖：C7 `TenantPathResolver`（attachments_root 已接，`agent/tools/path_resolver.py`）、C5 `_require_user_session`、
> C1 account→tenant→canonical conversation、webchat-durable-storage（canonical message / replay frames）。
> C12 §8.3 义务 owner = 本 change：落地 attachment blob root 时细化 manifest `tenant_workspace` 条目并复跑校验器。

## Why

§5.9.15 是剩余 P-1 设计门禁中已冻结、可开工的一项（P-1 出口条件要求附件解析上限 fail-closed，
不得再「由实现决定」）。现状（代码已核验 2026-10-02）：`POST /api/chat/uploads` 与
`GET /api/chat/media` 只做 session 门禁，走单用户 `AttachmentStore`（`~/.nexus/workspace/uploads`，
不可写时 fallback `/tmp/nexus_uploads`），上传响应回传**本地绝对路径**，`/media` 按**客户端可控 `path` 参数**
`expanduser().resolve()` 后仅以「在附件根下」判定放行——无 tenant ownership、无 MIME/size/像素/文本校验、
无 retention、无 reconciliation，blob 又落在 `/tmp`（§5.9.12 明确 `/tmp` 不属于 durable backup 范围）。
在 WebChat 面向受邀用户开放前，必须把这条链按 §5.9.15 冻结语义整体收口。

## What Changes

- **新 capability `attachment-media`**：上传只返回 immutable `attachment_id`；blob 落
  `<workspace>/attachments/{tenant_dirname}/{attachment_id}.{ext}`（复用 C7 同一根与 `_tenant_dirname` 清洗）；
  attachment metadata（owner/size/detected MIME/checksum/storage key/status/引用/retention deadline）落 PostgreSQL；
  上传/读取/转发/删除均重新校验 principal 与 ownership。
- **BREAKING（HTTP 契约）**：`/api/chat/uploads` 返回改为 `{"attachment_id", "url"}`（url 按 attachment_id 构造，
  不再回传本地 path）；`/api/chat/media` 改为 `?attachment_id=` + session 派生 tenant 读取，移除客户端可控 `path` 参数。
- **内容边界 fail-closed（冻结于 design，P-1 spec 列出精确清单）**：allowlist = `image/jpeg(.jpg`/`.jpeg)`、
  `image/png(.png)`、`image/webp(.webp)`、`image/gif(.gif)`、`text/plain(.txt)`（UTF-8，容 BOM）；
  扩展名与服务端 sniff **双一致**才准；单文件 ≤20 MiB；图片总像素 ≤16.8M（≈4096²）、解码内存 ≤64 MiB、
  解码超时 5s、GIF ≤100 帧；文本 ≤200k 字符、仅 UTF-8（拒 UTF-16/GBK 等并给明确错误）；
  PDF/压缩包/可执行内容/SVG/HTML/BMP/AVIF/HEIC 一律拒绝。错误码冻结（见 design ADR-9）。
- **引用与保留**：服务端登记 `message_attachments(message_id, attachment_id)`（§5.9.15 reference 落 PG；
  多消息可引用同一附件，转发更新 `last_referenced_at`）；已引用附件 30d 保留（自最后引用起算，对齐
  C12 30d operational 档），未引用/失败临时上传 24h 清理；orphan（blob 有 metadata 无）与 missing
  （metadata 有 blob 无）有 reconciliation 与告警。
- **移除 `/tmp` fallback**：`AttachmentStore._resolve_root` 的 `/tmp/nexus_uploads` 回退删除，blob 一律在
  workspace 内（§5.9.12 `/tmp` 不入 backup）。
- **backup manifest（C12 §8.3 义务）**：细化 `tests/fixtures/backup_manifest_template.json` 的
  `tenant-workspace` 条目（显式声明 attachment blob root 子路径、一致性点与 30d/24h 保留），
  复跑 `tests/backup_manifest` 校验器；不新增 kind（C12 kind 集合冻结）。
- **观测（C12 伴随落地协议）**：attachment upload/fetch/delete/cleanup 生命周期事件记录点，字段 ⊆
  既有单一来源（`work_queue_telemetry.ALLOWED_EVENT_FIELDS`），filename/本地路径/内容不入 label、
  不入普通日志（redaction）；C12 `tasks.md` §8.1 增量登记 + §8.3 勾选。

## Capabilities

### New Capabilities

- `attachment-media`：attachment 全生命周期——immutable `attachment_id` 上传/读取、tenant-namespaced
  blob、PG metadata 归属、fail-closed 内容边界（MIME/扩展名双一致 + size/像素/文本硬上限）、
  引用保留（30d 引用 / 24h 临时）、orphan/missing reconciliation、backup manifest 登记与 §7.1 观测记录点。

### Modified Capabilities

- 无。`auth-provisioning` Purpose 已声明 attachment 授权不属其范围（归 C6）；`webchat-protocol-dev-loop`
  协议帧 v0 不新增附件字段（见 design No-Goals），`webchat-durable-storage` 不改消息流断言。

## Impact

- **代码**：`bootstrap/chat_api.py`（uploads/media 端点改造，BREAKING）、`infra/channels/base.py`
  `AttachmentStore`（/tmp fallback 移除 + 新布局）、`infra/channels/web_chat_channel.py`（`save_upload`/
  `has_media`/`_can_read_media` 随端点上移）、`agent/tools/path_resolver.py`（attachments_root 与 blob root
  同一棵树，确认即可）；新增内容校验/仓储/清理模块（design 定位置）；迁移 `alembic/versions/`（新 revision，
  down_revision = `d8e4f2b6a9c1`，expand-only）。
- **数据库**：新增 `attachments`（metadata）+ `message_attachments`（引用）；不动既有表（ADR）。
- **配置**：新增 `[attachments]` 段（enabled/max_file_bytes/allowlist/资源上限/TTL），默认值 = 本 change 冻结值。
- **前端**：`npm run build:chat` 适配新上传/媒体契约（url 按 attachment_id）。
- **依赖**：Pillow（图片尺寸/像素探测与解码——检查既有依赖；若未引入为新增依赖）、标准库（mimetypes/magic 或
  python-magic——design 定，倾向纯标准库 + Pillow 探测）。

## Non-Goals

- **不实现 WebChat 协议帧 v0 的附件字段**（消息帧仍不含 attachments；引用登记服务端做，前端渲染后续 change）。
- **不改工具侧**：C7 已把文件工具（read_image_vision 等）接 tenant resolver 的 attachments_root，C6 不扩大
  工具面（工具读取仍走 resolver 租户隔离，不重复接 metadata 校验）。
- **不改 C12 kind 集合 / 不新增 manifest kind**（校验器锁定 postgresql/tenant_workspace/config/secrets）。
- **不进 C12 §8.4（observability retention 的 config 接线与总控台聚合 API）**：保持 P0 owner 不变。
- **不实现用户侧删除按钮/手动清理**：删除语义 = 解除引用后 retention 到期清理；管理员处置入口不在本 change。
- **不做服务端转码**（GBK/UTF-16 文本要求转码后重传）；**不做 SVG 光栅化准入**。