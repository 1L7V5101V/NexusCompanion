"""C6 attachment metadata + message attachment 引用（c6-attachment）

`attachments` / `message_attachments` 两表（openspec/changes/c6-attachment/design.md ADR-4）。

语义门禁：attachment-media spec——blob 落 tenant 命名空间、metadata 全量入 PG
（owner/size/detected MIME/checksum/storage key/status/引用/retention deadline）；
`message_attachments` 支持多消息引用同一附件（转发更新 last_referenced_at）。
`status` CHECK ∈ {staged, committed, missing}：staged = 上传中（staging 区，24h 清理，
rename 前不视为已提交）；committed = 已 rename 且 metadata 同事务提交；missing =
metadata 在但磁盘 blob 缺失（reconciliation 标记，读取返回 attachment_blob_missing）。
`retention_deadline` 双语义：staged 行 = created_at + 24h（临时清理）；committed 行 =
last_referenced_at + 30d（可配置）。纯 expand-only：downgrade 只 drop 两表，
不动任何既有表。

Revision ID: 000000000000_c6
Revises: d8e4f2b6a9c1
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e6f1a3b5c7d9"
down_revision: Union[str, Sequence[str], None] = "d8e4f2b6a9c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE attachments (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id          UUID         NOT NULL,
            tenant_id           VARCHAR(64)  NOT NULL,
            size_bytes          BIGINT       NOT NULL,
            detected_mime       VARCHAR(64)  NOT NULL,
            server_ext          VARCHAR(16)  NOT NULL,
            filename_display    VARCHAR(255) NOT NULL,
            checksum_sha256     CHAR(64)     NOT NULL,
            storage_key         VARCHAR(512) NOT NULL,
            status              VARCHAR(16)  NOT NULL DEFAULT 'staged',
            referencing_count   INTEGER      NOT NULL DEFAULT 0,
            last_referenced_at  TIMESTAMPTZ  NULL,
            retention_deadline  TIMESTAMPTZ  NOT NULL,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_attachments_storage_key UNIQUE (storage_key),
            CONSTRAINT ck_attachments_status CHECK (
                status IN ('staged', 'committed', 'missing')),
            CONSTRAINT ck_attachments_referencing_count CHECK (referencing_count >= 0),
            CONSTRAINT fk_attachments_account_id
                FOREIGN KEY (account_id)
                REFERENCES test_accounts (id) ON DELETE RESTRICT
        )
        """)
    op.execute("""
        CREATE INDEX ix_attachments_tenant_status
            ON attachments (tenant_id, status)
        """)
    op.execute("""
        CREATE INDEX ix_attachments_retention_deadline
            ON attachments (retention_deadline)
        """)
    op.execute("""
        CREATE TABLE message_attachments (
            message_id      UUID        NOT NULL,
            attachment_id   UUID        NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (message_id, attachment_id),
            CONSTRAINT fk_message_attachments_message_id
                FOREIGN KEY (message_id)
                REFERENCES canonical_messages (id) ON DELETE CASCADE,
            CONSTRAINT fk_message_attachments_attachment_id
                FOREIGN KEY (attachment_id)
                REFERENCES attachments (id) ON DELETE CASCADE
        )
        """)
    op.execute("""
        CREATE INDEX ix_message_attachments_attachment
            ON message_attachments (attachment_id)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS message_attachments")
    op.execute("DROP TABLE IF EXISTS attachments")