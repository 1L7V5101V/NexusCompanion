"""invite-code-tenant-registration: 邮箱密码凭据 + 租户邀请码列

（openspec/changes/invite-code-tenant-registration/design.md D1/D3）

- ``test_accounts``：+ ``email``（VARCHAR(255) UNIQUE NULL，存量行保持 NULL）、
  + ``password_digest``（TEXT NULL，argon2 哈希，存量行无密码）。
- ``access_tokens``：``account_id`` 解除 NOT NULL（签发租户邀请码时不依赖既有
  账号，FK 保留：消费回填后才指向账号）、+ ``tenant_name``（VARCHAR(255) NULL，
  管理员预指定租户名；旧 token 为 NULL 走旧兑换路径）。
- 唯一索引 ``uq_test_accounts_email`` 兜底邮箱全局唯一（并发注册竞态 → IntegrityError）。

downgrade 删列/恢复 NOT NULL 为安全操作（新列均为 NULLABLE，存量数据无损）。

Revision ID: c9d7e3a5f2b1
Revises: b7e2f9a4c1d8
Create Date: 2026-09-27
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c9d7e3a5f2b1"
down_revision: Union[str, Sequence[str], None] = "b7e2f9a4c1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE test_accounts ADD COLUMN email VARCHAR(255) NULL")
    op.execute("ALTER TABLE test_accounts ADD COLUMN password_digest TEXT NULL")
    # 邮箱全局唯一：唯一索引兜底并发注册竞态（设计 D1 email 唯一性）。
    op.execute(
        "CREATE UNIQUE INDEX uq_test_accounts_email ON test_accounts (email)"
    )
    # 账号状态枚举扩展 `failed`（注册 provisioning 失败态，非终态）：
    # 重定义 CHECK 约束允许 failed，支撑 spec「provisioning 失败 → 账号 failed」。
    op.execute("ALTER TABLE test_accounts DROP CONSTRAINT ck_test_accounts_status")
    op.execute(
        "ALTER TABLE test_accounts ADD CONSTRAINT ck_test_accounts_status CHECK "
        "(status IN ('provisioning', 'active', 'suspended', 'revoked', 'failed'))"
    )
    op.execute(
        "ALTER TABLE access_tokens ALTER COLUMN account_id DROP NOT NULL"
    )
    op.execute("ALTER TABLE access_tokens ADD COLUMN tenant_name VARCHAR(255) NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_test_accounts_email")
    op.execute("ALTER TABLE test_accounts DROP COLUMN IF EXISTS password_digest")
    op.execute("ALTER TABLE test_accounts DROP COLUMN IF EXISTS email")
    op.execute("ALTER TABLE access_tokens DROP COLUMN IF EXISTS tenant_name")
    op.execute(
        "ALTER TABLE access_tokens ALTER COLUMN account_id SET NOT NULL"
    )
    # 恢复原状态枚举（去掉 failed）。
    op.execute("ALTER TABLE test_accounts DROP CONSTRAINT ck_test_accounts_status")
    op.execute(
        "ALTER TABLE test_accounts ADD CONSTRAINT ck_test_accounts_status CHECK "
        "(status IN ('provisioning', 'active', 'suspended', 'revoked'))"
    )