# C6 attachment/media — design

> 对应 roadmap §5.9.15（Attachment/media lifecycle）与 §5.9.12（Persistence ownership / manifest）、
> §10 PROPOSED DEFAULT（Attachment policy，本 change 冻结）。行为契约见 `specs/attachment-media/spec.md`，
> 实施任务见 `tasks.md`。本文档冻结编码前决策，实现不得偏离；偏离需先改 design。

## 现状事实（design 引用的代码行）

| 域 | 事实 | 影响 |
| --- | --- | --- |
| `bootstrap/chat_api.py:274-291` | `POST /api/chat/uploads` 只做 session 门禁；`GET /api/chat/media?path=` 以客户端 path `expanduser().resolve()` + `_can_read_media`（`:348`）判定 | 无 tenant ownership、无内容校验、回显本地 path——本 change 废止 |
| `infra/channels/base.py:15-47` `AttachmentStore` | 根 `~/.nexus/workspace/uploads`；不可写时 fallback `/tmp/nexus_uploads`（`_resolve_root`）；`create_path` 用 uuid 前缀 | `/tmp` 回退违反 §5.9.12；文件名为 `chat_<uuid><ext>` 无租户维 |
| `infra/channels/web_chat_channel.py:305-316` | `save_upload` 返回 `{"path": str(path), "url": "/api/chat/media?path=…"}`（**直接把本地路径拼进 url**） | 泄漏面 = 本 change 根除对象 |
| `agent/tools/path_resolver.py:52-70` | 多租户 `workspace_root/tenants/<_tenant_dirname>/workspace`；`attachments_root`（`_category_root("attachments")`）已给工具用 | blob root 直接复用该树（`…/attachments/` 段），工具读 attachment 与 HTTP 读同一物理根 |
| `agent/tools/path_resolver.py:109-114` | `_tenant_dirname(tenant_id)` 跨平台清洗 + 8 位短哈希 | C6 storage key 复用同一函数保证与工具目录一致 |
| `alembic/versions/` | head = `d8e4f2b6a9c1`（webchat_replay_frames）；C15/C7 迁移链 `a7f2…→b3f7…→d8e4…` | C6 迁移 down_revision = `d8e4f2b6a9c1`（expand-only） |
| `core/backup/manifest.py` | kind 集合锁定 `{postgresql, tenant_workspace, config, secrets}`；`_is_temp_path` 拒绝 `/tmp`；模板 fixture `tests/fixtures/backup_manifest_template.json` 的 `tenant-workspace` 条目 notes 已写「C6 blob root 细化」 | C6 只细化该条目，不新增 kind、不改校验器行为 |
| `core/telemetry/label_policy.py` + `tests/fixtures/observability_event_schema.json` | metrics label 白名单 + 事件 schema 单一来源；C15 后 `work_queue_telemetry.ALLOWED_EVENT_FIELDS` 是事件字段单一来源 | C6 事件记录点字段 ⊆ 该单一来源，不扩 fixture、不扩白名单 |
| `core/telemetry/redaction.py` | `redact_text` 契约（secret/PII/路径占位） | 文件名/路径/内容入库前过 redaction |
| `requirements.txt:32` | `Pillow>=11.0.0` 已存在 | 图片尺寸/像素探测用 Pillow 懒加载 open + `Image.MAX_IMAGE_PIXELS` 显式上限，不新增解码依赖 |

## ADR-1 上传契约与路径泄漏根除

**冻结**：上传成功只返回 `{"attachment_id", "url"}`，`url = /api/chat/media?attachment_id=<uuid>`；`/api/chat/media`
改为按 `attachment_id` 读取且必须能解析到当前 session 派生 tenant 的归属（404 不泄露存在性）。删除 `save_upload`
返回本地 path 的路径；HTTP/WS/tool/channel 全链路不回显物理路径。旧 `/api/chat/media?path=` 参数直接拒绝（400）。

**备选**：继续容忍 `path` 参数 + `_can_read_media` 判定——不选：客户端可控路径天然无法 prove ownership，
且 `/media` 已带 session 门禁的事实只说明「有登录」不说明「有归属」。

## ADR-2 内容接受面：allowlist + 双一致 sniff（P-1 冻结值）

**冻结**（本 change 是所有值的唯一冻结点，配置化默认）：

| 项 | 冻结值 |
| --- | --- |
| 图片 allowlist | `image/jpeg`(.jpg/.jpeg)、`image/png`(.png)、`image/webp`(.webp)、`image/gif`(.gif) |
| 文本 allowlist | `text/plain`(.txt)，UTF-8（容 BOM） |
| 拒绝面 | PDF/压缩包(zip/gzip/tar/rar/7z)/可执行/脚本/HTML/SVG/BMP/AVIF/HEIC 一律拒绝 |
| 一致性 | 扩展名→期望 MIME + 服务端 sniff 判定 MIME 须**同时**在 allowlist 且**一致**，否则拒 |
| 单文件 | ≤ 20 MiB（读取 body 时按 Content-Length 与流式上限双防护，超限即断） |
| 图片资源 | 总像素 ≤ 16.8M（≈4096²）、解码内存 ≤ 64 MiB、解码超时 ≤ 5s、GIF 帧 ≤ 100 |
| 文本 | ≤ 200k 字符；UTF-8 严格解码（拒 UTF-16/GBK 等，提示转码重传） |

sniff 实现：纯标准库 magic-byte 表 + Pillow `Image.open`（懒加载，不改尺寸/不 decode）取 format 判定图片；
文本用 `bytes.decode('utf-8')` 成败判定。**不做** SVG 光栅化、不做拉链/解压试探（拒绝面直接拒，不解析）。

**备选**：引入 `python-magic`（libmagic）统一 sniff——不选：新增原生依赖与容器镜像依赖，Pilot 面窄，magic-byte +
Pillow 已覆盖 allowlist 全格式；libmagic 的商标/归属面（text 判定漂移）反而不利于 fail-closed 断言。

## ADR-3 storage key 与 blob 布局

**冻结**：blob root = C7 `TenantPathResolver.attachments_root`（多租户模式即
`{workspace}/tenants/{_tenant_dirname}/attachments/`，单机模式 `{workspace}/attachments/`）。
**工具读 attachment 与 HTTP 读 blob 是同一物理根**（ADR-9 不改工具侧：`read_image_vision`/
`read_file` 已能读到本租户上传附件），这是比目录形态更硬的约束。布局：

```
{attachments_root}
├── .staging/<upload_session_uuid>/   # 临时区：sniff+校验后先落此，24h TTL
└── <attachment_id>.<server_ext>       # server_ext 由 sniff 的 MIME 映射，不用客户端扩展名
```

- ``storage_key`` = ``{attachment_id}.{server_ext}``（**相对 attachments_root**；租户隔离由
  resolver 根天然提供，不再嵌套 tenant 段）；`attachment_id = uuid4()`，文件名不含客户端
  filename（filename 只存 metadata 展示名，不参与路径）。
- staging 落盘 → 校验全过 → `os.rename` 到最终路径（同文件系统原子移动）→ metadata 事务提交。
  两步之间进程崩溃留下 orphan（staging 超龄由 24h 清理；最终路径有 blob 无 metadata 由 reconciliation 清理）。
- **删除** `AttachmentStore` 的 `/tmp/nexus_uploads` fallback（`infra/channels/base.py`），单机/多租户一律
  workspace 内。
- 目录创建用 `mkdir(parents=True, exist_ok=True)`；staging 目录权限不 share，避免跨租户（同一进程单用户
  权限模型下主要靠 metadata 隔离，目录按 tenant 分开仍是纵深）。

**备选**：内容寻址 `sha256/<digest>`——不选：Pilot 单实例无去重需求，内容寻址会在「相同内容跨租户」时
引入共享 blob 的 ownership 复杂度，且 storage key 不再是 immutable id 的直接映射。

## ADR-4 metadata 表结构与引用登记（expand-only 迁移）

新 alembic revision（down_revision = `d8e4f2b6a9c1`），两张追加表：

`attachments`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID PK | = attachment_id（immutable，客户端可见 id） |
| account_id / tenant_id | FK + index | owner 双维度（§5.9.15 owner；tenant 用于隔离与路径派生） |
| size_bytes | BIGINT | |
| detected_mime | VARCHAR(64) | 服务端 sniff 结果（allowlist 内） |
| server_ext | VARCHAR(16) | 由 detected_mime 映射 |
| filename_display | VARCHAR(255) | 客户端原始名（仅展示，经 sanitize + redaction 面） |
| checksum_sha256 | CHAR(64) | blob 内容摘要 |
| storage_key | VARCHAR(512) | `{tenant_dirname}/{attachment_id}.{server_ext}`（相对 blob root） |
| status | VARCHAR(16) CHECK | `staged / committed / missing`（missing = metadata 在但 blob 不在） |
| referencing_count | INT | 引用数（= message_attachments 计数冗余，防扫描） |
| last_referenced_at | TIMESTAMPTZ | retention_deadline 结算锚点 |
| retention_deadline | TIMESTAMPTZ | = last_referenced_at + 30d（可配置）；staged 阶段 = created_at + 24h |
| created_at / updated_at | TIMESTAMPTZ | |

`message_attachments(message_id FK canonical_messages, attachment_id FK attachments, created_at, PK(message_id, attachment_id))`：
多消息引用同一附件 = 转发；每次 insert 更新 `last_referenced_at`/`referencing_count`/`retention_deadline`。

**rollback**：纯 expand-only，`alembic downgrade -1` 只 drop 两新表；旧端点行为（path 参数、本地 path 回显）
回滚 = revert 应用层 commit（不保留）。

## ADR-5 retention 与清理任务（幂等）

- **staged/临时**：`created_at + 24h` 到期；清理 = 删 staging 目录 + 删 `<attachment_id>` 未 commit 的孤儿 blob。
- **已引用**：`retention_deadline = last_referenced_at + 30d`（引用 insert 时重算）；解除全部引用（message
  删除/消息解除）后 `referencing_count=0` → deadline 从解除时点重算 30d。
- 清理任务（进程内定时 + 启动扫一次）：按 deadline 删 blob + metadata（先查后删，`DELETE … WHERE id=…` 幂等）；
  已引用项永不因清理任务删除（refcount>0 硬条件）。幂等验证：重复跑删除数 = 0。
- **reconciliation**（启动 + 可手动触发）：missing = metadata committed 但 blob 缺失 → status→`missing` + 结构化告警
  （不进普通日志正文）；orphan = blob 存在但无 metadata（含 staging 超龄）→ 删除。复用 C12 §8.4 的
  `core/telemetry/retention.py` 只报告不删除的 dry-run 形态做演练入口（不接线其 config，保持 P0 owner）。

**备选**：把清理并入 C12 §8.4 的 retention 总任务——不选：C12 §8.4 owner 已登记 P0，且 attachment TTL 是
内容生命周期不是观测 retention；独立任务 + 启动扫，职责单点。

## ADR-6 事件记录点（C12 伴随落地 §8.1 增量）

**冻结**：`bootstrap/` 新增 attachment 记录点模块，`upload.finished / fetch.finished / delete.finished /
cleanup.finished` 四类事件，字段**严格 ⊆ `work_queue_telemetry.ALLOWED_EVENT_FIELDS` 单一来源**
（不扩 fixture、不扩 label 白名单）：`status`（冻结枚举 `succeeded/failed/skipped`）、`error_type`、
`duration_ms`、`tenant_id`、`message_id`（有引用时）、派生 `queue_wait_ms` 不用（非队列语义，用直接耗时）。
**`attachment_id` 属高基数身份字段，不入 label 白名单**（与 §7.1 身份字段同规则）。filename/本地路径/内容
一律不入事件（共享 `redact_text` 兜底）。契约测试：字段 ⊆ util、label 白名单负向断言（`attachment_id` 注册被拒）。

**备选**：扩 `observability_event_schema.json` fixture 新增 attachment 事件类别——不选：C12 §8.1 的
「不得自造字段/不得扩源」约定在先，attachment 事件复用既有语义字段完全够表达，扩源只会重开三方对齐面。

## ADR-7 HTTP 端点迁移与错误码

`/api/chat/uploads`：读 body 前查 `Content-Length`（>20MiB 即 413）；流式读并累计（超限中断）；嗅探+校验 →
staging → commit → 返回 `{"attachment_id","url"}`。
`/api/chat/media`：`?attachment_id=` → session 派生 tenant → metadata 按 (id, tenant) 查 → blob 存在性 +
checksum 复算（读取前）→ FileResponse；`attachment_id` 不存在或跨租户 → 404；blob 缺失 → 404 + missing 标记。

冻结错误码（spec 面子集，HTTP 层映射）：

| 错误码 | HTTP | 语义 |
| --- | --- | --- |
| `upload_too_large` | 413 | >20MiB |
| `upload_type_denied` | 415 | 不在 allowlist |
| `upload_ext_mismatch` | 415 | sniff 与扩展名不一致 |
| `upload_pixel_limit` | 415 | 总像素 >16.8M |
| `upload_decode_timeout` | 415 | 解码/探测超时 |
| `upload_frames_limit` | 415 | GIF >100 帧 |
| `upload_text_limit` | 415 | 文本 >200k 字符 |
| `upload_encoding` | 415 | 非 UTF-8 |
| `attachment_not_found` / `attachment_forbidden` | 404 / 403 | 读取不存在 / 跨租户 |
| `attachment_blob_missing` | 404 | metadata 有 blob 无（触发 missing 标记） |

## ADR-8 backup manifest 条目细化（C12 §8.3 义务）

**冻结**：只改 `tests/fixtures/backup_manifest_template.json` 的 `tenant-workspace` 条目（kind/校验器不动）：
- `path` 明确含 `workspace/attachments/` 作为 attachment blob root 子路径（保留 tenant 命名空间说明）；
- `consistency_point`：与 PostgreSQL 恢复点对齐（attachment metadata 事务提交 = 一致性锚点；blob 在
  staging→rename 于同文件系统，rename 前不视为已提交）；
- `retention`：已引用 30 天 / 临时 24 小时（与 ADR-5 一致）；
- `checksum`：逐文件 sha256（与 attachment.checksum_sha256 同算法）；
- notes：`/tmp` 上传临时区已移除；`.staging/` 属临时区不属 durable backup。
复跑 `tests/backup_manifest` 校验器 + 契约测试断言；c12 `tasks.md` §8.3 勾选。

## ADR-9 工具侧保持 C7 现状（No-Goal 落点）

工具 `read_image_vision`/`read_file` 继续走 `TenantPathResolver.attachments_root` 的租户根（C7 已隔离），
**不**在本 change 把工具读取接 metadata/ownership 校验（工具能看到自己租户根内所有 blob，已含 attachment）。
C6 只保证 HTTP 面 ownership 与生命周期；工具面扩展留待 C14/后续按需。

## 风险 / 权衡

- [客户端可控 path 的旧 url 已可能存在于前端缓存/书签] → media 旧参数直接 400；前端 build:chat 同步（本 change 内）。
- [Pillow 解码是 C 扩展，单一大图可占用大量内存] → 先尺寸头探测（不 decode），超 16.8M 像素即拒；解码放
  线程池 + 超时（5s）包裹，不阻塞事件循环。
- [gif 帧数探测需循环 seek，慢] → 最多 seek 到 100 帧即断（上限本身即目标，超出即拒）；帧数探测在尺寸
  校验通过后执行。
- [rename 与 metadata 提交非原子（两快照）] → 崩溃窗口只产生 orphan/missing 一态，由 reconciliation 收敛；
  不在单事务内迁移 blob（文件系统无事务）。
- [DELETE 附件的 endpoint 不在 Pilot 用户面] → 本 change 只实现解除引用/清理；显式删除端点留给后续管理面。
- [manifest 模板 fixture 被多测试引用] → 改动同步契约测试断言；复跑全量 backup manifest 组。

## 测试策略

- 校验核心单测：allowlist 双一致矩阵（每种 allowlist 类型 × 伪装扩展名 × 拒绝面样本）、像素/帧/超时/编码
  边界、`upload_*` 错误码稳定。
- 仓储单测（PG scratch，NEXUS_REQUIRE_PG=1）：引用 insert/解除、deadline 重算、refcount、跨租户 404、
  清理幂等、orphan/missing reconciliation。
- HTTP 契约测试：uploads/media 新契约 + 旧 path 参数拒绝 + `_require_user_session` 延续。
- E10 契约测试：事件字段 ⊆ 单一来源 + label 白名单负向（`attachment_id` 拒）。
- manifest：更新模板 + 校验器 + 契约测试通过。
- 回归：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/` + pyright（project + tests）零新增。

## Open Questions

- 无（§5.9.15 门禁收口完成后无「由实现决定」遗留；前端附件渲染为 No-Goal，不影响本 change 行为契约）。