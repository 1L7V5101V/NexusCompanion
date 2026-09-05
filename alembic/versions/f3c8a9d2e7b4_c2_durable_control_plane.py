"""C2 durable control plane tables

inbox_records / message_deduplication_keys / turns / tool_calls /
background_work_items / outbound_delivery_intents / delivery_attempts 七表
（openspec/changes/c2-durable-control-plane/design.md §3 冻结 DDL）。

语义门禁：PILOT_ROADMAP §5.9.6（durable state 与 restart recovery）、§5.9.9
（实体清单/唯一约束/首次启用）、§5.9.11（三事务边界、幂等双键、lease/退避/
dead_letter）、§10 DECIDED（三事务边界不可合并；sent 必须由 ack 推进；不导入
旧单体数据）。seed：not_applicable（dev tenant 无存量 work，见 design §3 注记）。

首次启用 = Create → Verify → Enable；rollback = 关闭 Pilot 入口并保留 PG 数据。
C2 尚未 cutover，downgrade 删除七张新表是安全操作（§5.9.9）。

Revision ID: f3c8a9d2e7b4
Revises: c4d8f2a6e9b3
Create Date: 2026-09-06
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f3c8a9d2e7b4"
down_revision: Union[str, Sequence[str], None] = "c4d8f2a6e9b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE message_deduplication_keys (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          VARCHAR(64)  NOT NULL,
            source_channel     VARCHAR(64)  NULL,
            source_identity_id VARCHAR(255) NULL,
            source_message_id  VARCHAR(255) NULL,
            account_id         UUID         NULL,
            client_message_id  VARCHAR(255) NULL,
            created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_message_dedup_keys_key_present CHECK (
                (source_message_id IS NOT NULL AND source_channel IS NOT NULL
                 AND source_identity_id IS NOT NULL)
                OR (client_message_id IS NOT NULL AND account_id IS NOT NULL)),
            CONSTRAINT fk_message_dedup_keys_account_id
                FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT
        )
        """)
    # 部分唯一索引：PG 对含 NULL 的普通唯一索引不生效，必须用谓词索引表达
    # 「该键存在时唯一」（design.md ADR-2）。
    op.execute("""
        CREATE UNIQUE INDEX uq_message_dedup_keys_source
            ON message_deduplication_keys (source_channel, source_identity_id, source_message_id)
            WHERE source_message_id IS NOT NULL
        """)
    op.execute("""
        CREATE UNIQUE INDEX uq_message_dedup_keys_client
            ON message_deduplication_keys (account_id, client_message_id)
            WHERE client_message_id IS NOT NULL
        """)
    op.execute("""
        CREATE TABLE inbox_records (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id            VARCHAR(64) NOT NULL,
            conversation_id      UUID        NOT NULL,
            dedup_key_id         UUID        NOT NULL,
            canonical_message_id UUID        NOT NULL,
            status               VARCHAR(32) NOT NULL DEFAULT 'accepted',
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at         TIMESTAMPTZ NULL,
            CONSTRAINT uq_inbox_records_dedup_key_id UNIQUE (dedup_key_id),
            CONSTRAINT fk_inbox_records_conversation_id
                FOREIGN KEY (conversation_id)
                REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_inbox_records_dedup_key_id
                FOREIGN KEY (dedup_key_id)
                REFERENCES message_deduplication_keys (id) ON DELETE RESTRICT,
            CONSTRAINT fk_inbox_records_canonical_message_id
                FOREIGN KEY (canonical_message_id)
                REFERENCES canonical_messages (id) ON DELETE RESTRICT,
            CONSTRAINT ck_inbox_records_status CHECK (status IN ('accepted', 'processed'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_inbox_records_conversation
            ON inbox_records (tenant_id, conversation_id, created_at)
        """)
    op.execute("""
        CREATE TABLE turns (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        VARCHAR(64) NOT NULL,
            conversation_id  UUID        NOT NULL,
            inbox_record_id  UUID        NULL,
            status           VARCHAR(32) NOT NULL DEFAULT 'queued',
            error_json       TEXT        NULL,
            final_message_id UUID        NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_turns_conversation_id
                FOREIGN KEY (conversation_id)
                REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_turns_inbox_record_id
                FOREIGN KEY (inbox_record_id)
                REFERENCES inbox_records (id) ON DELETE RESTRICT,
            CONSTRAINT fk_turns_final_message_id
                FOREIGN KEY (final_message_id)
                REFERENCES canonical_messages (id) ON DELETE RESTRICT,
            CONSTRAINT ck_turns_status CHECK (
                status IN ('queued', 'in_progress', 'completed', 'interrupted', 'failed', 'cancelled'))
        )
        """)
    op.execute("""
        CREATE UNIQUE INDEX uq_turns_inbox_record_id
            ON turns (inbox_record_id) WHERE inbox_record_id IS NOT NULL
        """)
    op.execute("CREATE INDEX ix_turns_tenant_status ON turns (tenant_id, status)")
    op.execute("""
        CREATE TABLE tool_calls (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    VARCHAR(64)  NOT NULL,
            turn_id      UUID         NOT NULL,
            tool_name    VARCHAR(255) NOT NULL,
            status       VARCHAR(32)  NOT NULL DEFAULT 'running',
            outcome_json TEXT         NULL,
            started_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            finished_at  TIMESTAMPTZ  NULL,
            CONSTRAINT fk_tool_calls_turn_id
                FOREIGN KEY (turn_id) REFERENCES turns (id) ON DELETE RESTRICT,
            CONSTRAINT ck_tool_calls_status CHECK (
                status IN ('running', 'succeeded', 'failed', 'cancelled', 'unknown'))
        )
        """)
    op.execute("CREATE INDEX ix_tool_calls_turn ON tool_calls (turn_id, started_at)")
    op.execute("""
        CREATE TABLE background_work_items (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       VARCHAR(64)  NOT NULL,
            conversation_id UUID         NULL,
            work_kind       VARCHAR(64)  NOT NULL,
            status          VARCHAR(32)  NOT NULL DEFAULT 'queued',
            idempotency_key VARCHAR(255) NULL,
            payload_json    TEXT         NULL,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            finished_at     TIMESTAMPTZ  NULL,
            CONSTRAINT uq_background_work_items_idempotency_key UNIQUE (idempotency_key),
            CONSTRAINT fk_background_work_items_conversation_id
                FOREIGN KEY (conversation_id)
                REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
            CONSTRAINT ck_background_work_items_status CHECK (
                status IN ('queued', 'in_progress', 'succeeded', 'failed', 'cancelled'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_background_work_items_tenant_status
            ON background_work_items (tenant_id, status)
        """)
    op.execute("""
        CREATE TABLE outbound_delivery_intents (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        VARCHAR(64)  NOT NULL,
            conversation_id  UUID         NOT NULL,
            message_id       UUID         NOT NULL,
            turn_id          UUID         NULL,
            idempotency_key  VARCHAR(255) NOT NULL,
            channel          VARCHAR(64)  NOT NULL,
            target_chat_id   VARCHAR(255) NOT NULL,
            payload_json     TEXT         NULL,
            status           VARCHAR(32)  NOT NULL DEFAULT 'pending',
            attempt_count    INTEGER      NOT NULL DEFAULT 0,
            lease_owner      VARCHAR(128) NULL,
            lease_expires_at TIMESTAMPTZ  NULL,
            next_attempt_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
            last_error       TEXT         NULL,
            sent_at          TIMESTAMPTZ  NULL,
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_outbound_delivery_intents_idempotency_key UNIQUE (idempotency_key),
            CONSTRAINT fk_outbound_delivery_intents_conversation_id
                FOREIGN KEY (conversation_id)
                REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_outbound_delivery_intents_message_id
                FOREIGN KEY (message_id)
                REFERENCES canonical_messages (id) ON DELETE RESTRICT,
            CONSTRAINT fk_outbound_delivery_intents_turn_id
                FOREIGN KEY (turn_id) REFERENCES turns (id) ON DELETE RESTRICT,
            CONSTRAINT ck_outbound_delivery_intents_status CHECK (
                status IN ('pending', 'attempting', 'sent', 'failed', 'dead_letter'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_outbound_delivery_intents_claim
            ON outbound_delivery_intents (status, next_attempt_at)
        """)
    op.execute("""
        CREATE TABLE delivery_attempts (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            intent_id        UUID         NOT NULL,
            outcome          VARCHAR(32)  NOT NULL,
            provider_receipt VARCHAR(255) NULL,
            error            TEXT         NULL,
            started_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            finished_at      TIMESTAMPTZ  NULL,
            CONSTRAINT fk_delivery_attempts_intent_id
                FOREIGN KEY (intent_id)
                REFERENCES outbound_delivery_intents (id) ON DELETE RESTRICT,
            CONSTRAINT ck_delivery_attempts_outcome CHECK (outcome IN ('sent', 'failed', 'redrive'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_delivery_attempts_intent
            ON delivery_attempts (intent_id, started_at)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS delivery_attempts")
    op.execute("DROP TABLE IF EXISTS outbound_delivery_intents")
    op.execute("DROP TABLE IF EXISTS background_work_items")
    op.execute("DROP TABLE IF EXISTS tool_calls")
    op.execute("DROP TABLE IF EXISTS turns")
    op.execute("DROP TABLE IF EXISTS inbox_records")
    op.execute("DROP TABLE IF EXISTS message_deduplication_keys")
