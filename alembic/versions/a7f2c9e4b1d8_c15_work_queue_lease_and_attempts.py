"""C15 work queue lease columns + work_attempts audit stream

为 work item 消费者补 claim 所需列与生命周期审计流
（openspec/changes/2026-09-20-c15-work-queue-consumer/design.md ADR-2）：

- `background_work_items` 增 `attempt_count` / `lease_owner` / `lease_expires_at` /
  `next_attempt_at` / `last_error` —— 与 `outbound_delivery_intents` 的 lease 列
  **同名同语义**；另增 `flow`（业务链路，决定 handler）——`work_kind` 决定 lane，
  `flow` 决定 handler，两者不复用（ADR-2/ADR-5）；
- `ix_background_work_items_claim (status, next_attempt_at)` —— claim 扫描路径；
  既有 `(tenant_id, status)` 索引保留给租户过滤查询；
- `work_attempts` —— 只追加生命周期审计流（镜像 `delivery_attempts`），承载
  `succeeded` / `failed` / `released` / `recovered` / `redrive` 五类留痕，使死信
  （`failed`）可事后复盘；redrive 不删不改本表（ADR-2 定案 (ii)）。

语义门禁：PILOT_ROADMAP §5.9.9（后续 PostgreSQL schema evolution 按
expand/backfill/cutover）。本迁移**只做 expand**：新列全部有 DEFAULT 或可 NULL，
既有行无需回填，旧代码（不指定新列的 INSERT）不受影响 ⇒ 迁移与 worker 接线可分开发布
（ADR-7 Rollout）。

状态 CHECK **不变**（ADR-1：沿用 queued/in_progress/succeeded/failed/cancelled，不引入
pending/processing/done）。`flow` **不加** CHECK（可扩展枚举，漂移风险由 C12 的
`observability_event_schema.json` + 代码校验承担）；`work_attempts.outcome` **加** CHECK
（枚举封闭，与 `delivery_attempts.outcome` 同规格）。seed：not_applicable。

rollback：`DROP TABLE work_attempts` + `DROP INDEX` + 6 个 `DROP COLUMN`；无 backfill
数据需要撤销；已 succeeded/failed 的行保留（仅失去 lease/flow 列）。

注：本 revision 只存在于未推送的 C15 feature 分支，尚未进 `main` / 未部署，因此直接
改写（保持「一个 change 一个 migration」的仓库惯例，对齐 C2 的七表单迁移），而非叠加
第二个 migration。

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
            ADD COLUMN last_error       TEXT         NULL,
            ADD COLUMN flow             VARCHAR(32)  NULL
        """)
    op.execute("""
        CREATE INDEX ix_background_work_items_claim
            ON background_work_items (status, next_attempt_at)
        """)
    op.execute("""
        CREATE TABLE work_attempts (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            work_item_id UUID         NOT NULL,
            outcome      VARCHAR(32)  NOT NULL,
            error        TEXT         NULL,
            started_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            finished_at  TIMESTAMPTZ  NULL,
            CONSTRAINT fk_work_attempts_work_item_id
                FOREIGN KEY (work_item_id)
                REFERENCES background_work_items (id) ON DELETE RESTRICT,
            CONSTRAINT ck_work_attempts_outcome CHECK (
                outcome IN ('succeeded', 'failed', 'released', 'recovered', 'redrive'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_work_attempts_item
            ON work_attempts (work_item_id, started_at)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS work_attempts")
    op.execute("DROP INDEX IF EXISTS ix_background_work_items_claim")
    op.execute("""
        ALTER TABLE background_work_items
            DROP COLUMN IF EXISTS flow,
            DROP COLUMN IF EXISTS last_error,
            DROP COLUMN IF EXISTS next_attempt_at,
            DROP COLUMN IF EXISTS lease_expires_at,
            DROP COLUMN IF EXISTS lease_owner,
            DROP COLUMN IF EXISTS attempt_count
        """)
