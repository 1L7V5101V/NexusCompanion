"""partition memory_items by tenant_id + native vector embedding

Rebuilds memory_items as a LIST-partitioned table keyed by tenant_id so
that per-partition HNSW indexes work (decision B, see
docs/tasks/phase1-storage/vector-validation.md).  embedding moves from
TEXT to native vector(1024); malformed legacy text embeddings become NULL.

Revision ID: a3d5c7e9f1b2
Revises: d6e1cd9205cd
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a3d5c7e9f1b2"
down_revision: Union[str, Sequence[str], None] = "d6e1cd9205cd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = """tenant_id, id, memory_type, summary, content_hash, embedding,
reinforcement, emotional_weight, extra_json, source_ref, happened_at,
status, created_at, updated_at"""


def _sanitize_ident(value: str) -> str:
    """分区名只保留字母/数字/下划线，防止 tenant_id 注入标识符。"""
    import re

    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. 重建为分区表（LIST by tenant_id）──────────────
    #     LIST 分区要求主键包含分区键。
    op.execute(
        """
        CREATE TABLE memory_items_new (
            tenant_id VARCHAR(64) NOT NULL,
            id VARCHAR(64) NOT NULL,
            memory_type VARCHAR(64) NOT NULL,
            summary TEXT NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            embedding vector(1024),
            reinforcement INTEGER NOT NULL DEFAULT 1,
            emotional_weight INTEGER NOT NULL DEFAULT 0,
            extra_json TEXT,
            source_ref VARCHAR(255),
            happened_at TIMESTAMPTZ,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id)
        ) PARTITION BY LIST (tenant_id)
        """
    )

    # ── 2. 为已存在的 tenant 显式建分区 ──────────────────
    #     不建 DEFAULT 分区：默认分区会混装多租户，破坏决策 B 的
    #     每分区 HNSW 隔离（见 vector-validation.md）。新租户由
    #     PostgresMemoryStore 写入时动态建分区。
    rows = conn.execute(
        sa.text(
            "SELECT DISTINCT tenant_id FROM memory_items "
            "WHERE tenant_id IS NOT NULL"
        )
    ).fetchall()
    for (tid,) in rows:
        part = f"memory_items_{_sanitize_ident(tid)}"
        op.execute(
            sa.text(
                f"CREATE TABLE {part} PARTITION OF memory_items_new "
                "FOR VALUES IN (:tid)"
            ).bindparams(tid=tid)
        )

    # ── 3. 迁移数据：embedding text -> vector，非法值置 NULL ──
    op.execute(
        """
        CREATE FUNCTION pg_temp.try_vector(t text) RETURNS vector
        LANGUAGE plpgsql AS $$
        BEGIN
            RETURN t::vector;
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END $$;
        """
    )
    op.execute(
        f"""
        INSERT INTO memory_items_new ({_COLUMNS})
        SELECT tenant_id, id, memory_type, summary, content_hash,
               pg_temp.try_vector(embedding),
               reinforcement, emotional_weight, extra_json, source_ref,
               happened_at, status, created_at, updated_at
        FROM memory_items
        """
    )

    # ── 4. 父表 HNSW 索引（各分区自动继承，决策 B 验证参数）──
    op.execute(
        """
        CREATE INDEX ix_memory_items_embedding_hnsw ON memory_items_new
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )

    # ── 5. 换名（先 drop 旧表，释放旧索引名）───────────────
    op.execute("DROP TABLE memory_items")
    op.execute("ALTER TABLE memory_items_new RENAME TO memory_items")

    # ── 6. 其余索引（父表建，分区自动继承；旧名此刻已释放）──
    op.execute(
        "CREATE UNIQUE INDEX ix_memory_items_content_hash "
        "ON memory_items (tenant_id, content_hash, memory_type)"
    )
    op.execute(
        "CREATE INDEX ix_memory_items_type_status "
        "ON memory_items (tenant_id, memory_type, status)"
    )

    # ── 7. 多租户修复 ────────────────────────────────
    #    consolidation_events 的 source_ref 在初始迁移中是全局主键，
    #    跨 tenant 相同 source_ref 会撞 PK（错误跳过 consolidation），
    #    改为 (tenant_id, source_ref) 复合主键。
    op.execute(
        "ALTER TABLE consolidation_events DROP CONSTRAINT consolidation_events_pkey"
    )
    op.execute(
        "ALTER TABLE consolidation_events ADD PRIMARY KEY (tenant_id, source_ref)"
    )
    #    memory_replacements 缺 source_ref 列（SQLite 有，初始迁移漏了），
    #    record_replacements 依赖该列。
    op.execute(
        "ALTER TABLE memory_replacements ADD COLUMN IF NOT EXISTS source_ref VARCHAR(255)"
    )


def downgrade() -> None:
    op.execute(
        "CREATE TABLE memory_items_old ("
        "tenant_id VARCHAR(64) NOT NULL, "
        "id VARCHAR(64) NOT NULL, "
        "memory_type VARCHAR(64) NOT NULL, "
        "summary TEXT NOT NULL, "
        "content_hash VARCHAR(64) NOT NULL, "
        "embedding TEXT, "
        "reinforcement INTEGER NOT NULL DEFAULT 1, "
        "emotional_weight INTEGER NOT NULL DEFAULT 0, "
        "extra_json TEXT, "
        "source_ref VARCHAR(255), "
        "happened_at TIMESTAMPTZ, "
        "status VARCHAR(32) NOT NULL DEFAULT 'active', "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")"
    )
    op.execute(
        "INSERT INTO memory_items_old (tenant_id, id, memory_type, summary, "
        "content_hash, embedding, reinforcement, emotional_weight, extra_json, "
        "source_ref, happened_at, status, created_at, updated_at) "
        "SELECT tenant_id, id, memory_type, summary, content_hash, "
        "embedding::text, reinforcement, emotional_weight, extra_json, "
        "source_ref, happened_at, status, created_at, updated_at "
        "FROM memory_items"
    )
    op.execute("DROP TABLE memory_items")
    op.execute("ALTER TABLE memory_items_old RENAME TO memory_items")

    # 还原第 7 步的多租户修复
    op.execute(
        "ALTER TABLE consolidation_events DROP CONSTRAINT consolidation_events_pkey"
    )
    op.execute(
        "ALTER TABLE consolidation_events ADD PRIMARY KEY (source_ref)"
    )
    op.execute(
        "ALTER TABLE memory_replacements DROP COLUMN IF EXISTS source_ref"
    )
