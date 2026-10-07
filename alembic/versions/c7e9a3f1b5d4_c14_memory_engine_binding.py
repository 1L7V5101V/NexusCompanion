"""C14 memory engine catalog：tenant_memory_engine_bindings / events 两表
（c14-memory-engine-catalog ADR-1）

- `tenant_memory_engine_bindings`：每 tenant 恰一行 active memory engine binding。
  §5.9.16 memory engine slot 为 required（不能关闭，可在允许目录内切换），
  「始终且仅有一个 active engine」由 **tenant_id 主键**直接强制（一行即一个
  active），`tenant_policy_revision` 从 0 起每次成功切换 +1（供 TenantRuntimePlan
  以 `r<n>` 消费）。
- `tenant_memory_engine_events`：initial/switch 历史按 `(tenant_id, engine_id)`
  可追踪（验收「切换不迁移数据、记录可追踪」）；切换不迁移/不删除旧引擎数据，
  本表只记录 binding 变更本身。

纯 expand-only：downgrade 只 drop 两表，不动任何既有表。

Revision ID: c7e9a3f1b5d4
Revises: f5a9c1e3b7d2
Create Date: 2026-10-07
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c7e9a3f1b5d4"
down_revision: Union[str, Sequence[str], None] = "f5a9c1e3b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tenant_memory_engine_bindings (
            tenant_id              VARCHAR(64)  PRIMARY KEY,
            engine_id              VARCHAR(64)  NOT NULL,
            tenant_policy_revision BIGINT       NOT NULL DEFAULT 0,
            updated_by             VARCHAR(64)  NOT NULL DEFAULT '',
            created_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_tmeb_engine_id CHECK (engine_id <> '')
        )
        """)
    op.execute("""
        CREATE TABLE tenant_memory_engine_events (
            id                     BIGSERIAL    PRIMARY KEY,
            tenant_id              VARCHAR(64)  NOT NULL,
            engine_id              VARCHAR(64)  NOT NULL,
            action                 VARCHAR(16)  NOT NULL,
            tenant_policy_revision BIGINT       NOT NULL,
            actor                  VARCHAR(64)  NOT NULL DEFAULT '',
            created_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_tmee_action CHECK (action IN ('initial', 'switch'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_tmee_tenant_engine
            ON tenant_memory_engine_events (tenant_id, engine_id, created_at)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_memory_engine_events")
    op.execute("DROP TABLE IF EXISTS tenant_memory_engine_bindings")
