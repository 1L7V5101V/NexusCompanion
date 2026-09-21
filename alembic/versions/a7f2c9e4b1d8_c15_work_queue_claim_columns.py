"""C15 background_work_items claim/lease columns

为 work item 消费者补 claim 所需列（openspec/changes/2026-09-20-c15-work-queue-consumer/
design.md ADR-2）：

- `attempt_count` / `lease_owner` / `lease_expires_at` / `next_attempt_at` /
  `last_error` —— 与 `outbound_delivery_intents` 的 lease 列**同名同语义**，使两表
  的认领模式共用同一套心智模型与测试模式；
- `ix_background_work_items_claim (status, next_attempt_at)` —— claim 扫描路径；
  既有 `(tenant_id, status)` 索引保留给租户过滤查询。

语义门禁：PILOT_ROADMAP §5.9.9（首次 Create → Verify → Enable；后续 PostgreSQL
schema evolution 按 expand/backfill/cutover）。本迁移**只做 expand**：新列全部有
DEFAULT 或可 NULL，既有行无需回填，旧代码（不指定新列的 INSERT）不受影响；因此
迁移与 worker 接线可分开发布（先迁移后接线），符合 ADR-7 Rollout。

状态 CHECK **不变**（design ADR-1：沿用 queued/in_progress/succeeded/failed/
cancelled，不引入 pending/processing/done）。seed：not_applicable（无存量 work
需要回填）。

rollback：纯 additive，downgrade = DROP INDEX + DROP COLUMN；无 backfill 数据需要
撤销；已 succeeded/failed 的行保留（仅失去 lease 列）。

Revision ID: a7f2c9e4b1d8
Revises: f3c8a9d2e7b4
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a7f2c9e4b1d8"
down_revision: Union[str, Sequence[str], None] = "f3c8a9d2e7b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE background_work_items
            ADD COLUMN attempt_count    INTEGER      NOT NULL DEFAULT 0,
            ADD COLUMN lease_owner      VARCHAR(128) NULL,
            ADD COLUMN lease_expires_at TIMESTAMPTZ  NULL,
            ADD COLUMN next_attempt_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
            ADD COLUMN last_error       TEXT         NULL
        """)
    op.execute("""
        CREATE INDEX ix_background_work_items_claim
            ON background_work_items (status, next_attempt_at)
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_background_work_items_claim")
    op.execute("""
        ALTER TABLE background_work_items
            DROP COLUMN IF EXISTS last_error,
            DROP COLUMN IF EXISTS next_attempt_at,
            DROP COLUMN IF EXISTS lease_expires_at,
            DROP COLUMN IF EXISTS lease_owner,
            DROP COLUMN IF EXISTS attempt_count
        """)
