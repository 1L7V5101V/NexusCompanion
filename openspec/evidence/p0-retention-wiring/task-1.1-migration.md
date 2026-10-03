# task-1.1 / 1.2 — 迁移与模型同步（ADR-4）

日期：2026-10-03　分支：`feature/p0-retention-wiring`　worktree：`D:/1/wt-retention`

## 变更

| 文件 | 内容 |
| --- | --- |
| `alembic/versions/b8e2f4a6c0d2_retention_digest_nullable.py` | **新**（expand-only）：`auth_sessions.session_digest` / `access_tokens.token_digest` DROP NOT NULL；`ck_access_tokens_digest_sha256` 放宽为 `token_digest IS NULL OR char_length(token_digest)=64`；两张表 UNIQUE 不动。downgrade 先断言无 NULL digest 残留（不静默清理），再恢复原约束 |
| `bootstrap/db/models/auth.py` | `token_digest` / `session_digest` 改 `Mapped[str \| None]`（nullable），注 NULL = purge 已清除语义 |

## head 核对

实现前 `alembic heads` = `e6f1a3b5c7d9`（C6 attachment media），非 tasks.md 初稿写的值；
`down_revision` 取实测 head。

## 迁移闭环（真实 PG 5433 便携实例，`nexus` 库）

```
DATABASE_URL=postgresql+psycopg://nexus:nexus_dev@127.0.0.1:5433/nexus alembic upgrade head
  → Running upgrade e6f1a3b5c7d9 -> b8e2f4a6c0d2
alembic downgrade -1
  → Running downgrade b8e2f4a6c0d2 -> e6f1a3b5c7d9
alembic upgrade head
  → Running upgrade e6f1a3b5c7d9 -> b8e2f4a6c0d2
alembic current → b8e2f4a6c0d2 (head)
```

注：`localhost` 在本机 psycopg 下先试 `::1` 超时，验证统一用 `127.0.0.1`（环境事实，非迁移问题）。

## 约束形状（psycopg 直查 pg_catalog）

```
('access_tokens',  'token_digest',   'YES')     # 可空 ✓
('auth_sessions',  'session_digest', 'YES')     # 可空 ✓
ck_access_tokens_digest_sha256 → CHECK (((token_digest IS NULL) OR (char_length((token_digest)::text) = 64)))  ✓
uq_access_tokens_digest        → UNIQUE (token_digest)      # 保持不动 ✓
uq_auth_sessions_digest        → UNIQUE (session_digest)    # 保持不动 ✓
```

## pyright（task 1.2 验证项）

改动文件 `pyright --level error` 结果并入 `task-8.3-pyright.txt`（第 8 节统一跑）。
