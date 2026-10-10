"""C11 explicit schedules：scheduled_jobs / schedule_executions 两表
（c11-explicit-schedules design ADR-1）

- `scheduled_jobs`：显式用户 schedule 的 durable job 行——owner 三元组
  （tenant_id / account_id / conversation_id，§5.9.14）+ 服务端解析冻结的
  delivery binding（`delivery_channel`/`delivery_target`，模型/客户端不可提交
  授权目标）+ 规格（trigger/tier/schedule_spec_json/IANA timezone/revision）+
  调度态（status、next_scheduled_for）。
- `schedule_executions`：一次名义 occurrence 的执行记录，`(job_id,
  scheduled_for)` 唯一幂等（§10 DECIDED）；持久化 attempt、terminal outcome
  （running → succeeded/failed；missed/skipped 直落终态）、skip 原因、触发时
  时区/计划版本快照与产出的 delivery intent/message 引用（FK RESTRICT：
  execution 留痕不随 delivery 清理丢失）。

**既有同名表处置**：`d6e1cd9205cd`（rachael_and_extras）曾建过旧 `scheduled_jobs`
（String PK + channel/chat_id 旧模型，`scripts/import_to_pg.py` 一次性导入旧
`schedules.json` 的派生拷贝），**无任何运行时读取方**（全库零代码引用，仅导入
脚本写入）。旧 schema 与 §5.9.14 owner 模型不兼容且无法可信映射到
account/conversation 三元组（ADR-7：不猜测归属、不自动迁移），故本迁移将其
**重命名为 `scheduled_jobs_import_legacy`** 保留（不静默销毁；确认无用后的
DROP 属运维决策，不在 schema 迁移里做），腾出规范表名。downgrade 对称回退。

除上述重命名外纯 expand-only：downgrade 只 drop 本 change 两表，不动任何既有表
（outbound_delivery_intents 等 C2 表零改动——本表只按 intent 模式消费）。

Revision ID: e8b4c2a6d9f1
Revises: c7e9a3f1b5d4
Create Date: 2026-10-07
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e8b4c2a6d9f1"
down_revision: Union[str, Sequence[str], None] = "c7e9a3f1b5d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE IF EXISTS scheduled_jobs
            RENAME TO scheduled_jobs_import_legacy
        """)
    op.execute("""
        CREATE TABLE scheduled_jobs (
            id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          VARCHAR(64)  NOT NULL,
            account_id         UUID         NOT NULL REFERENCES test_accounts (id) ON DELETE RESTRICT,
            conversation_id    UUID         NOT NULL REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
            name               VARCHAR(255),
            trigger_kind       VARCHAR(16)  NOT NULL,
            tier               VARCHAR(16)  NOT NULL,
            schedule_spec_json TEXT         NOT NULL,
            message            TEXT,
            prompt             TEXT,
            timezone           VARCHAR(64)  NOT NULL,
            revision           INTEGER      NOT NULL DEFAULT 1,
            delivery_channel   VARCHAR(64)  NOT NULL,
            delivery_target    VARCHAR(255) NOT NULL,
            status             VARCHAR(16)  NOT NULL DEFAULT 'active',
            next_scheduled_for TIMESTAMPTZ,
            last_outcome       VARCHAR(16),
            created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_scheduled_jobs_trigger CHECK (trigger_kind IN ('at', 'after', 'every')),
            CONSTRAINT ck_scheduled_jobs_tier CHECK (tier IN ('instant', 'soft')),
            CONSTRAINT ck_scheduled_jobs_status CHECK (status IN ('active', 'suspended', 'revoked')),
            CONSTRAINT ck_scheduled_jobs_last_outcome CHECK (
                last_outcome IS NULL OR last_outcome IN ('succeeded', 'failed', 'missed', 'skipped')
            )
        )
        """)
    op.execute("""
        CREATE TABLE schedule_executions (
            id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id             UUID         NOT NULL REFERENCES scheduled_jobs (id) ON DELETE RESTRICT,
            tenant_id          VARCHAR(64)  NOT NULL,
            scheduled_for      TIMESTAMPTZ  NOT NULL,
            status             VARCHAR(16)  NOT NULL DEFAULT 'running',
            attempt_count      INTEGER      NOT NULL DEFAULT 0,
            skip_reason        VARCHAR(64),
            error              TEXT,
            schedule_timezone  VARCHAR(64)  NOT NULL,
            schedule_revision  INTEGER      NOT NULL,
            delivery_intent_id UUID         REFERENCES outbound_delivery_intents (id) ON DELETE RESTRICT,
            delivery_message_id UUID        REFERENCES canonical_messages (id) ON DELETE RESTRICT,
            started_at         TIMESTAMPTZ,
            finished_at        TIMESTAMPTZ,
            created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_schedule_executions_job_scheduled UNIQUE (job_id, scheduled_for),
            CONSTRAINT ck_schedule_executions_status CHECK (
                status IN ('running', 'succeeded', 'failed', 'missed', 'skipped')
            )
        )
        """)
    op.execute("""
        CREATE INDEX ix_scheduled_jobs_due
            ON scheduled_jobs (status, next_scheduled_for)
        """)
    op.execute("""
        CREATE INDEX ix_scheduled_jobs_tenant_status
            ON scheduled_jobs (tenant_id, status)
        """)
    op.execute("""
        CREATE INDEX ix_scheduled_jobs_account_status
            ON scheduled_jobs (account_id, status)
        """)
    op.execute("""
        CREATE INDEX ix_schedule_executions_status_time
            ON schedule_executions (status, scheduled_for)
        """)
    op.execute("""
        CREATE INDEX ix_schedule_executions_tenant
            ON schedule_executions (tenant_id, created_at)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS schedule_executions")
    op.execute("DROP TABLE IF EXISTS scheduled_jobs")
    op.execute("""
        ALTER TABLE IF EXISTS scheduled_jobs_import_legacy
            RENAME TO scheduled_jobs
        """)
