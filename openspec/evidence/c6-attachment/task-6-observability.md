# Task 6 evidence — 观测记录点与 redaction（C12 §8.1 伴随落地）

- 实现：`bootstrap/attachments/telemetry.py`（AttachmentTelemetry + 4 指标族）、
  `bootstrap/attachments/service.py`（upload/fetch 内部成功/失败记录点、
  delete 事件入口）、`bootstrap/attachments/lifecycle.py`（cleanup.finished →
  telemetry.cleanup_finished 接线已由 `_emit` 注入）、`bootstrap/chat_api.py`
  （Service 注入 build_default_attachment_telemetry；移除端点层重复 emit）、
  `bootstrap/app.py`（lifecycle 注入 telemetry）。
- 测试：`tests/attachments/test_telemetry.py`（8）。

## 回归输出

```
uv run pytest tests/attachments/ tests/test_chat_api.py tests/test_webchat_telemetry.py
  tests/test_work_queue_telemetry.py
  → 91 passed
uv run pyright --level error bootstrap/attachments/ bootstrap/chat_api.py
  bootstrap/app.py tests/attachments/
  → 0 errors
openspec validate c6-attachment                → valid
openspec validate c12-observability-backup     → valid
```

## 契约验证（对 task 6.1 场景）

- 四类事件 upload/fetch/delete/cleanup .finished 产出字段 ⊆
  `work_queue_telemetry.ALLOWED_EVENT_FIELDS`（`test_*_within_fixture` 断言）；
  `ALLOWED_EVENT_FIELDS == fixture` 双向断言不扩单一来源；
- **size_bytes 不在白名单**（fixture 无该字段）→ 从事件字段移除，量化走
  `attachment_uploads_total` counter（不扩事件面）；
- redaction 兜底：error 自由文本过 `core/telemetry/redaction.redact_text`
  （`sk-…` secret 形被掩码，`test_upload_error_event_redacts_free_text`）；
- label 白名单负向：`attachment_id`/`message_id` 注册被
  `MetricLabelPolicyError` 拒绝（`test_metric_labels_within_policy`）；
- 记录点异常不阻断主流程（emit 全 try/except 守护）；
- 四指标族注册 + 计数断言（`test_metric_counters_increment`）。

## C12 登记

- `c12-observability-backup/tasks.md` §8.1 追加 C6 attachment 事件记录点落地
  记录（模块/端点/契约测试/负向）；§8.3 已随 task 5.2 勾选。