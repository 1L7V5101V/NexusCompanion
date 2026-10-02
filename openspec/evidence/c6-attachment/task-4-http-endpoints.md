# Task 4 evidence — HTTP 端点改造（BREAKING）与前端适配

- 实现：`bootstrap/chat_api.py`（uploads/media 新契约 + `_resolve_endpoint_identity` +
  `_upload_error_status`）、`bootstrap/attachments/service.py`（编排层）、
  `bootstrap/app.py`（attachment_config 装配）、`infra/channels/base.py`
  （AttachmentStore /tmp fallback 移除）、`infra/channels/web_chat_channel.py`
  （save_upload/has_media/upload_roots 旧 path 面删除）。
- 测试：`tests/attachments/test_http_api.py`（8，真 PG + AuthRuntime）、
  `tests/test_chat_api.py`（dev 模式 503/400 语义改写）、`tests/test_channel_base.py`。

## 回归输出

```
uv run pytest tests/attachments/ tests/test_chat_api.py tests/test_channel_base.py
  → 58 passed （attachments 全组 + chat_api + channel_base）
uv run pyright --level error bootstrap/chat_api.py bootstrap/app.py
  bootstrap/attachments/service.py infra/channels/web_chat_channel.py
  infra/channels/base.py tests/attachments/test_http_api.py tests/test_chat_api.py
  → 0 errors
```

## 契约验证点（对 spec scenarios）

- 上传成功 → `{"attachment_id","url"}`，url 不含 path/storage_key/C:/tmp/workspace；
  上传→读取 e2e 字节一致且 content-type = detected MIME；
- 旧 `?path=` 参数 → 400（任何模式）；跨租户/不存在 attachment_id → 404；
- Content-Length >20MiB → 413 `upload_too_large`；banned 魔数 → 415
  `upload_type_denied`；扩展名/内容不一致 → 415 `upload_ext_mismatch`；
- dev 模式（无 auth+PG durable）→ uploads/media 503 fail-closed，不落任何
  单用户路径（assert 无 workspace/uploads、workspace/attachments 产生）；
- AttachmentStore `/tmp/nexus_uploads` fallback 移除：root 不可写直接抛错；
  web_chat_channel 不再回显本地 path（旧方法已删）。

## 前端适配

`frontend/chat` 当前无上传/附件调用（附件渲染 = Non-Goal，后续 change）；`npm run
build:chat` 构建通过（index.html + js bundle 产出 static/chat/，.gitignore 不跟踪）。
协议 none：wire 帧未新增附件字段，WebChat 协议常量未改，前端契约无变化。

## 关键测试基建

- `tests/attachments/test_http_api.py` 建真 AuthRuntime（scratch PG，
  cookie_secure=False → `nexus_session` cookie 名）+ 直造 user session
  （CredentialRepository.create_user_session）验证 _require_user_session 闭环；
- 关键坑记录：cookie_secure 默认 True 时 cookie 名带 `__Host-` 前缀 → dev 测试必须
  显式 cover 为 False；httpx2 对 per-request cookies 的 DeprecationWarning 在
  `-W error` 下会中止 → cookie 直接注入 client.headers。