# C6 attachment/media — 实施任务

> 对应 `openspec/changes/c6-attachment/`；design.md ADR-1..ADR-9 为实现约束，偏离需先改 design。
> 分支：`feature/c6-attachment`（worktree 建议 `D:/1/wt-nexus-c6`）。
> 证据统一落 `openspec/evidence/c6-attachment/`。
> 阶段：P0/P1（附件生命周期 + 公网前必做内容边界）；伴随落地协议：本 change checklist 必须携带
> 「§7.1 指标字段 + redaction + backup manifest 条目」条目（C12 协议，见 §6/§7）。

## 1. DB 迁移与 ORM model（ADR-4）

- [x] 1.1 新增 alembic revision（`down_revision = d8e4f2b6a9c1`，当前 head）：build `attachments`
  （id UUID PK / account_id / tenant_id index / size_bytes / detected_mime / server_ext /
  filename_display / checksum_sha256 CHAR(64) / storage_key / status CHECK∈{staged,committed,missing} /
  referencing_count / last_referenced_at / retention_deadline / created_at / updated_at）+ `message_attachments`
  （message_id FK canonical_messages, attachment_id FK attachments, PK(message_id, attachment_id)）。
  验证：`alembic upgrade head` + `downgrade -1` + `upgrade head` 可逆（真 PG 验证）；expand-only 兼容性
  （旧列只写不受影响）
- [x] 1.2 ORM model 同步（`bootstrap/db/models/`）：`AttachmentModel` / `MessageAttachmentModel`；
  仓储 seim 复用 `control_plane.py` 的 session 工厂。验证：model `__table__` 断言 + pyright 0 errors

## 2. 内容校验核心（ADR-2）

- [ ] 2.1 实现 `bootstrap/attachments/validation.py`：allowlist 表（MIME↔扩展名双向映射 + server_ext 映射）、
  magic-byte sniff（jpeg/png/webp/gif 头 + Pillow `Image.open` 懒加载 format 双重印证）、文本 UTF-8 严格解码
  （容 BOM）、单文件 ≤20MiB 判断、总像素 ≤16.8M / GIF 帧 ≤100（seek 断）、解码超时 5s（线程池 + 超时包裹）。
  验证：`tests/attachments/test_validation.py` 矩阵——每种 allowlist 类型正例 × 伪装扩展名、
  拒绝面样本（PDF/zip/exe/svg/html/bmp/avif/heic）、`upload_type_denied`/`upload_ext_mismatch`/
  `upload_pixel_limit`/`upload_frames_limit`/`upload_decode_timeout`/`upload_text_limit`/
  `upload_encoding` 错误码稳定
- [ ] 2.2 文本上限（≤200k 字符）与编码错误码（`upload_text_limit`/`upload_encoding`）冻结并测试。
  验证：同一校验函数对 200001 字符 UTF-8 文本拒绝；UTF-16/GBK 字节拒绝且错误码为 `upload_encoding`

## 3. AttachmentRepository / Service（ADR-3 / ADR-4 / ADR-5）

- [ ] 3.1 `AttachmentRepository`（PG，tenant 维度）：`create_attachment`（staged）→ `commit_attachment`
  （rename 后置 committed + checksum/storage_key）、`get_owned(account, tenant, attachment_id)`、
  `add_reference(message_id, attachment_id)` / `remove_reference`（更新 referencing_count /
  last_referenced_at / retention_deadline）、`mark_missing`、`list_expired`（staged>24h 或
  committed refcount=0 且 deadline 过期）。验证：PG scratch（NEXUS_REQUIRE_PG=1）：引用 insert 更新
  deadline、解除全部引用后 refcount=0、跨租户查询返回空、CAS 幂等
- [ ] 3.2 staging→committed 两段式：staging 落 `{root}/.staging/<uuid>/` → 校验全过 → `os.rename` 到
  `{root}/{tenant_dirname}/{attachment_id}.{server_ext}` → 同会话 metadata 提交。验证：进程崩溃窗口
  （模拟 rename 后未提交 metadata）产生 orphan，由 reconciliation 收敛；storage_key 与磁盘路径一致
- [ ] 3.3 清理任务（幂等）：`cleanup_expired()` 删 staging 超龄 + committed 到期（refcount=0 硬条件）；
  `reconcile()` 标记 missing（committed 无 blob）+ 清理 orphan（有 blob 无 metadata）。验证：重复跑
  第二次删除数=0；missing 项被标记且读取返回 `attachment_blob_missing`；已引用附件永不误删

## 4. HTTP 端点改造（ADR-1 / ADR-7）— BREAKING

- [ ] 4.1 `POST /api/chat/uploads`：Content-Length 预检（>20MiB→413）+ 流式读累计超限中断 + 校验 →
  返回 `{"attachment_id","url"}`；移除本地 path 回显。验证：`tests/attachments/test_http_api.py` 新契约
  （响应无 path、url 按 attachment_id）
- [ ] 4.2 `GET /api/chat/media`：改按 `?attachment_id=` + session 派生 tenant 校验归属 + blob/checksum 核验
  → FileResponse；旧 `?path=` 参数 400；跨租户/不存在 404；blob 缺失触发 missing 标记并 404。
  验证：跨租户负向、revoked 账号负向（复用 C8/fail-closed 形态）、旧参数拒绝
- [ ] 4.3 前端适配：`npm run build:chat` 上传/媒体调用改新契约。验证：前端构建通过 + 上传→读取 e2e
  （dev + PG 双模式）

## 5. 清理与 reconciliation 接线（ADR-5 / ADR-8）

- [ ] 5.1 进程内定时清理 + 启动 reconciliation 接线（`bootstrap/app.py` 生命周期，参照
  `pg-durable-sot-cutover` 启动恢复模式）；dry-run 演练形态复用 `core/telemetry/retention.py` 契约。
  验证：启动扫一次 + 定时触发；dry-run 只报告不删除的负向断言
- [ ] 5.2 backup manifest 细化（C12 §8.3 义务）：`tests/fixtures/backup_manifest_template.json`
  `tenant-workspace` 条目补 attachment blob root 子路径、一致性点（PG 恢复点对齐）、retention 30d/24h、
  逐文件 sha256；`.staging/` 与 `/tmp` 声明为临时区不入 backup。验证：`tests/backup_manifest` 校验器
  + 契约测试通过，且 C12 `c12-observability-backup/tasks.md` §8.3 登记勾选

## 6. 观测记录点与 redaction（C12 §8.1 伴随落地）

- [ ] 6.1 `bootstrap/attachments/telemetry.py`：`upload.finished`/`fetch.finished`/`delete.finished`/
  `cleanup.finished` 四类事件，字段严格 ⊆ `work_queue_telemetry.ALLOWED_EVENT_FIELDS`（单一来源，不扩
  fixture/白名单）；filename/路径/内容一律不入事件（redact_text 兜底）。验证：契约测试双向断言
  （字段 ⊆ 单一来源、不得自造）+ label 白名单负向（`attachment_id` 注册被拒）
- [ ] 6.2 C12 登记：`c12-observability-backup/tasks.md` §8.1 增量登记本 change 为 attachment 事件记录点
  owner；§8.3 随 5.2 勾选。验证：`openspec validate c12-observability-backup` 通过

## 7. 测试闸门与证据

- [ ] 7.1 全量回归：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/`（含 tests/attachments 全组）全绿；
  pyright `--level error` project + tests 两配置零新增。验证：evidence `openspec/evidence/c6-attachment/
  task-7.1-regression.txt`（回归计数与基线对照）
- [ ] 7.2 checklist 回填（仅在有 evidence 时）：PILOT_ROADMAP_PROJECT_CHECKLIST —— §5.9.15 门禁行
  标记收口（附本 change 指针）、P1「HTTP/上传/媒体统一认证」条目勾选（附件租户归属与生命周期归 C6
  落定）、next decision 移除 C6 提名项。验证：checklist 状态与 evidence 指针一致