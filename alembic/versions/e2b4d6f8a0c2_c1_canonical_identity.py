"""C1 canonical identity tables

test_accounts / canonical_conversations / canonical_messages 基础表
（openspec/changes/2026-09-05-c1-canonical-identity/design.md §3 冻结 DDL）。

语义门禁：PILOT_ROADMAP §5.9.2（account→tenant→canonical conversation 1:1、
per-conversation 0-based sequence）、§5.9.9（约束/首次启用）、§10 DECIDED
（不导入旧单体数据；本 migration 只建表 + dev seed，无任何旧库读取）。

首次启用 = Create → Verify → Enable；rollback = 关闭 Pilot 入口并保留 PG 数据。
C1 尚未 cutover，downgrade 删除三张新表是安全操作（§5.9.9）。

Revision ID: e2b4d6f8a0c2
Revises: b6e9d2c4a8f1
Create Date: 2026-09-05
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e2b4d6f8a0c2"
down_revision: Union[str, Sequence[str], None] = "b6e9d2c4a8f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# dev seed 固定 UUID（确定性，便于 Verify 阶段与测试断言）。
DEV_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
DEV_CONVERSATION_ID = "00000000-0000-0000-0000-000000000002"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE test_accounts (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    VARCHAR(64)   NOT NULL,
            status       VARCHAR(32)   NOT NULL DEFAULT 'provisioning',
            display_name VARCHAR(255)  NOT NULL DEFAULT '',
            created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
            CONSTRAINT uq_test_accounts_tenant_id UNIQUE (tenant_id),
            CONSTRAINT ck_test_accounts_status CHECK (
                status IN ('provisioning', 'active', 'suspended', 'revoked'))
        )
        """)
    op.execute("""
        CREATE TABLE canonical_conversations (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     VARCHAR(64)  NOT NULL,
            account_id    UUID         NOT NULL,
            status        VARCHAR(32)  NOT NULL DEFAULT 'active',
            next_sequence BIGINT       NOT NULL DEFAULT 0,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_canonical_conversations_tenant_id UNIQUE (tenant_id),
            CONSTRAINT fk_canonical_conversations_account_id
                FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT,
            CONSTRAINT ck_canonical_conversations_status CHECK (
                status IN ('active', 'archived'))
        )
        """)
    op.execute("""
        CREATE TABLE canonical_messages (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          VARCHAR(64)  NOT NULL,
            conversation_id    UUID         NOT NULL,
            sequence           BIGINT       NOT NULL,
            role               VARCHAR(32)  NOT NULL,
            content            TEXT         NULL,
            source_channel     VARCHAR(64)  NULL,
            source_identity_id VARCHAR(255) NULL,
            source_message_id  VARCHAR(255) NULL,
            client_message_id  VARCHAR(255) NULL,
            metadata_json      TEXT         NULL,
            created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_canonical_messages_conversation_sequence
                UNIQUE (conversation_id, sequence),
            CONSTRAINT fk_canonical_messages_conversation_id
                FOREIGN KEY (conversation_id)
                REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
            CONSTRAINT ck_canonical_messages_role CHECK (
                role IN ('user', 'assistant', 'system', 'tool'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_canonical_messages_tenant_conversation
            ON canonical_messages (tenant_id, conversation_id, sequence)
        """)
    # dev seed：P0.5 dev 闭环与测试在 C5 provisioning 之前使用的显式单用户规范身份。
    # tenant_id 显式命名为 'dev'（与单体默认租户名刻意区分）；生产账号一律由
    # provisioning 创建。
    op.execute(f"""
        INSERT INTO test_accounts (id, tenant_id, status, display_name)
        VALUES ('{DEV_ACCOUNT_ID}', 'dev', 'active', 'Pilot Dev Account')
        ON CONFLICT (tenant_id) DO NOTHING
        """)
    op.execute(f"""
        INSERT INTO canonical_conversations (id, tenant_id, account_id, status)
        VALUES ('{DEV_CONVERSATION_ID}', 'dev', '{DEV_ACCOUNT_ID}', 'active')
        ON CONFLICT (tenant_id) DO NOTHING
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS canonical_messages")
    op.execute("DROP TABLE IF EXISTS canonical_conversations")
    op.execute("DROP TABLE IF EXISTS test_accounts")
