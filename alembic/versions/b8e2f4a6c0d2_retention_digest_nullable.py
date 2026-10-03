"""Retention 凭据摘要置空：digest 列放开 NOT NULL + CHECK 放宽（p0-retention-wiring）

p0-retention-wiring ADR-4（expand-only）：过期凭据处置 = 「已撤销 ∧ 已过期 ∧ 超过
purge_grace_s」的 `auth_sessions.session_digest` / `access_tokens.token_digest`
UPDATE 为 NULL（凭据不可再用），行本身与归属/时间 metadata 保留供审计核验。

- 两列 NOT NULL → 可空；两张表的 UNIQUE（`uq_auth_sessions_digest` /
  `uq_access_tokens_digest`）保持不动：PostgreSQL 默认 NULLS DISTINCT，
  多行已清除记录可共存（tests/retention/test_credential_purge.py 固化）。
- `ck_access_tokens_digest_sha256` 由 `char_length(token_digest)=64` 放宽为
  `token_digest IS NULL OR char_length(token_digest)=64`（auth_sessions 的
  session_digest 本就没有长度 CHECK，不需动）。
- 按摘要等值查找的校验路径（validate_session 等）拿不到 NULL 匹配，天然 fail-closed。

downgrade 断言无 NULL digest 残留后才恢复 NOT NULL（不静默清理——删行会破坏
§5.9.12「不物理删除审计链」；残留已清除行属运维先决问题，报错人工处置）。

Revision ID: b8e2f4a6c0d2
Revises: e6f1a3b5c7d9
Create Date: 2026-10-03
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b8e2f4a6c0d2"
down_revision: Union[str, Sequence[str], None] = "e6f1a3b5c7d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auth_sessions ALTER COLUMN session_digest DROP NOT NULL"
    )
    op.execute("ALTER TABLE access_tokens ALTER COLUMN token_digest DROP NOT NULL")
    op.execute(
        "ALTER TABLE access_tokens DROP CONSTRAINT ck_access_tokens_digest_sha256"
    )
    op.execute("""
        ALTER TABLE access_tokens ADD CONSTRAINT ck_access_tokens_digest_sha256
            CHECK (token_digest IS NULL OR char_length(token_digest) = 64)
        """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM auth_sessions WHERE session_digest IS NULL)
               OR EXISTS (SELECT 1 FROM access_tokens WHERE token_digest IS NULL)
            THEN
                RAISE EXCEPTION
                    'cannot downgrade b8e2f4a6c0d2: NULL credential digests present; '
                    'resolve purged rows first (audit rows must not be deleted silently)';
            END IF;
        END
        $$;
        """)
    op.execute(
        "ALTER TABLE access_tokens DROP CONSTRAINT ck_access_tokens_digest_sha256"
    )
    op.execute("""
        ALTER TABLE access_tokens ADD CONSTRAINT ck_access_tokens_digest_sha256
            CHECK (char_length(token_digest) = 64)
        """)
    op.execute(
        "ALTER TABLE auth_sessions ALTER COLUMN session_digest SET NOT NULL"
    )
    op.execute("ALTER TABLE access_tokens ALTER COLUMN token_digest SET NOT NULL")
