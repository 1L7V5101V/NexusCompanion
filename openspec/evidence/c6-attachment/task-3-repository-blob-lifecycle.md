# Task 3.1–3.3 evidence — AttachmentRepository / blob 两段式 / 生命周期清理

- 实现：`bootstrap/db/repository/attachment_repo.py`、`bootstrap/attachments/blob_store.py`、
  `bootstrap/attachments/lifecycle.py`
- 测试：`tests/attachments/test_attachment_repo.py`（10）、`test_blob_store.py`（9）、
  `test_lifecycle.py`（3），真 PG scratch DB `nexus_c6test`（alembic upgrade head 含 C6 表）+
  文件系统层
- pyright `--level error`：0 errors

## 回归输出

```
uv run pytest tests/attachments/ -q -p no:cacheprovider   → 42 passed in 27.24s
uv run pyright --level error bootstrap/attachments/ bootstrap/db/repository/attachment_repo.py tests/attachments/
  → 0 errors, 0 warnings
```

## 覆盖要点（对应 spec scenarios）

- create→commit 两段式（staged→committed CAS，双 commit 拒绝）；storage_key 与磁盘一致
  （uuid hex 无连字符文件名），blob 逃逸拒绝（嵌套/越根/非 allowlist ext）；
- 引用：add_reference 更新 refcount + deadline；解除全部引用 refcount=0 且 deadline 重算；
  跨账号/跨租户查询与更新返回 None（404 不泄露存在性）；
- cleanup：到期 + refcount=0 硬条件删除（已引用永不误删），删除幂等（第二轮 0）；staging
  超龄（older_than）清理幂等；
- reconcile：committed 无 blob → status=missing（读取 404 语义）；孤儿 blob（无 metadata）
  清理并保留 known；崩壊窗口 = rename 完成 metadata 未提交 → 孤儿可收敛；
- /tmp 立场：blob root 恒在调用方（workspace）内，无 /tmp fallback。

## 涉及 design 澄清

apply 期间修订 design ADR-3：blob 布局从 `{workspace}/attachments/{tenant_dirname}/…` 统一定为
C7 `TenantPathResolver.attachments_root`（租户命名空间根，`{blob_root}/{attachment_id}.{ext}`，
storage_key 相对该根）——保证「工具读 attachment 与 HTTP 读 blob 同一物理根」（ADR-9 不改工具侧），
spec/proposal 已同步，openspec validate 通过。