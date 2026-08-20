"""session tables multi-tenant PK + pg_trgm full-text index

Sessions / messages were created by the initial migration with key / id as
globally-unique primary keys. Under single-db multi-tenancy (decision C) the
same session key can exist for different tenants, so the PK must include
tenant_id. Also fixes messages UNIQUE(session_key, seq) and adds the pg_trgm
extension + GIN index that PostgresSessionStore.search_messages uses (SQLite
uses FTS5 trigram; pg_trgm is the closest PG equivalent for CJK substring
matching).

Revision ID: b6e9d2c4a8f1
Revises: a3d5c7e9f1b2
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b6e9d2c4a8f1"
down_revision: Union[str, Sequence[str], None] = "a3d5c7e9f1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. sessions / messages 复合主键（决策 C：同 key 可跨 tenant 共存）──
    op.execute("ALTER TABLE sessions DROP CONSTRAINT sessions_pkey")
    op.execute("ALTER TABLE sessions ADD PRIMARY KEY (tenant_id, key)")

    op.execute("ALTER TABLE messages DROP CONSTRAINT messages_pkey")
    # message id = f"{session_key}:{seq}"，session_key 最长 255，需放宽长度
    op.execute("ALTER TABLE messages ALTER COLUMN id TYPE VARCHAR(511)")
    op.execute("ALTER TABLE messages ADD PRIMARY KEY (tenant_id, id)")

    # ── 2. messages 唯一约束 (tenant_id, session_key, seq) ──────────────
    #     SQLite 基线是 UNIQUE(session_key, seq)；现有非唯一索引改为唯一。
    op.execute("DROP INDEX ix_messages_tenant_session")
    op.execute(
        "CREATE UNIQUE INDEX ix_messages_tenant_session_seq "
        "ON messages (tenant_id, session_key, seq)"
    )

    # ── 3. pg_trgm：search_messages 全文检索 ────────────────────────────
    #     SQLite 用 FTS5 trigram；pg_trgm 的 GIN 索引同时加速 ILIKE '%x%'，
    #     中文按 UTF-8 三元组子串匹配，语义最接近（tsvector 需中文分词扩展）。
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_messages_content_trgm "
        "ON messages USING gin (content gin_trgm_ops)"
    )


def downgrade() -> None:
    # pg_trgm 扩展是集群级依赖，可能被其他对象引用，降级不 drop，仅去索引。
    op.execute("DROP INDEX ix_messages_content_trgm")
    op.execute("DROP INDEX ix_messages_tenant_session_seq")
    op.execute(
        "CREATE INDEX ix_messages_tenant_session "
        "ON messages (tenant_id, session_key, seq)"
    )
    op.execute("ALTER TABLE messages DROP CONSTRAINT messages_pkey")
    op.execute("ALTER TABLE messages ADD PRIMARY KEY (id)")
    op.execute("ALTER TABLE messages ALTER COLUMN id TYPE VARCHAR(64)")

    op.execute("ALTER TABLE sessions DROP CONSTRAINT sessions_pkey")
    op.execute("ALTER TABLE sessions ADD PRIMARY KEY (key)")
