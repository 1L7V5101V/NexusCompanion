# Task 1.1 evidence — alembic migration（attachments + message_attachments）双向可逆

- 时间：2026-10-02
- 环境：本地 PG（postgresql+psycopg://nexus:nexus_dev@localhost:5432/nexus）
- revision：`e6f1a3b5c7d9`（down_revision = `d8e4f2b6a9c1`）

## 验证 1：upgrade head

```
Running upgrade d8e4f2b6a9c1 -> e6f1a3b5c7d9, C6 attachment metadata ...
C6 tables: [('attachments',), ('message_attachments',)]
current head: e6f1a3b5c7d9
```

## 验证 2：downgrade -1（纯 expand-only 可逆）

```
Running downgrade e6f1a3b5c7d9 -> d8e4f2b6a9c1
after downgrade tables: []
head: d8e4f2b6a9c1
```

## 验证 3：再 upgrade head

```
final head: e6f1a3b5c7d9
```

## 结论

- 两表在 `public` schema 创建成功：`attachments`（含 storage_key UNIQUE、status CHECK∈{staged,committed,missing}、referencing_count>=0 CHECK、account FK RESTRICT）、`message_attachments`（PK(message_id, attachment_id)，CASCADE 双向 FK，attachment 侧索引 `ix_message_attachments_attachment`）。
- 索引：`ix_attachments_tenant_status`、`ix_attachments_retention_deadline`。
- downgrade 只 drop 两表，不动任何既有表；可逆闭环确认。