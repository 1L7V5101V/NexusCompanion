# Task 1.2 evidence — ORM model 同步 + model↔migration 契约测试

- 文件：`bootstrap/db/models/attachment.py`（AttachmentModel / MessageAttachmentModel，
  ATTACHMENT_STATUSES 枚举）、`bootstrap/db/models/__init__.py` 导出。
- 契约测试：`tests/attachments/test_model_contract.py`（4 passed）——列集合 ↔ migration、
  message_attachments 列 ↔ migration、status CHECK 枚举一致性、storage_key UNIQUE 声明。
- pyright `--level error`：0 errors（model 模块 + alembic revision + 测试）。

## 输出

```
uv run pytest tests/attachments/test_model_contract.py -q   → 4 passed in 0.48s
uv run pyright --level error ...                            → 0 errors, 0 warnings
```

## model 列（与 migration e6f1a3b5c7d9 一致）

attachments: account_id, checksum_sha256, created_at, detected_mime, filename_display,
id, last_referenced_at, referencing_count, retention_deadline, server_ext, size_bytes,
status, storage_key, tenant_id, updated_at；indexes = ix_attachments_retention_deadline,
ix_attachments_tenant_status。
message_attachments: attachment_id, created_at, message_id。