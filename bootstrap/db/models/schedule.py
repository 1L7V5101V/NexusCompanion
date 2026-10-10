"""C11 显式用户 schedule 模型（c11-explicit-schedules design ADR-1）。

owner 三元组（tenant/account/conversation）+ 服务端解析冻结的 delivery binding
（§5.9.14：模型/客户端 SHALL NOT 提交任意 channel/chat_id 作为授权目标）；
execution 以 `(job_id, scheduled_for)` 唯一幂等（§10 DECIDED），持久化 attempt、
terminal outcome、skip 原因与触发时的时区/计划版本快照。本模块只新增两表——
C2 的 `outbound_delivery_intents` schema 零改动（仅按 intent 模式消费）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base

SCHEDULE_JOB_STATUSES = ("active", "suspended", "revoked")
"""job 调度态（§5.9.14 / §10 DECIDED）：active 调度、suspended 暂停新执行
（恢复后按 misfire 规则从下一未来 occurrence 继续）、revoked 禁用。"""

SCHEDULE_EXECUTION_STATUSES = ("running", "succeeded", "failed", "missed", "skipped")
"""execution 状态机：running → succeeded/failed；missed（one-shot 超 grace）与
skipped（recurring 前进/binding 失效等）直落终态，不经历 running。"""

SCHEDULE_SKIP_REASONS = (
    "misfire_grace_exceeded",
    "recurring_advance",
    "binding_inactive",
    "revocation_gate",
    "delivery_target_unresolved",
)
"""skip/miss 原因词汇（可扩展；每条 skipped/missed 记录必有原因，不留无声空洞）。"""


class ScheduledJobModel(Base):
    """显式用户 schedule durable job 行（§5.9.6 P1 必收对象）。

    `delivery_channel`/`delivery_target` 是**创建时服务端解析并冻结**的授权目标
    （WebChat native：`chat`/`<tenant_id>`；telegram：`telegram`/`<chat_id>`），
    后续不可被工具调用覆盖。`revision` 是计划版本（语义变更 +1），与
    `schedule_executions.schedule_revision` 配合实现「触发时快照可解释」。
    """

    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        CheckConstraint(
            "trigger_kind IN ('at', 'after', 'every')",
            name="ck_scheduled_jobs_trigger",
        ),
        CheckConstraint("tier IN ('instant', 'soft')", name="ck_scheduled_jobs_tier"),
        CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')",
            name="ck_scheduled_jobs_status",
        ),
        CheckConstraint(
            "last_outcome IS NULL OR last_outcome IN "
            "('succeeded', 'failed', 'missed', 'skipped')",
            name="ck_scheduled_jobs_last_outcome",
        ),
        Index("ix_scheduled_jobs_due", "status", "next_scheduled_for"),
        Index("ix_scheduled_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_scheduled_jobs_account_status", "account_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "test_accounts.id",
            ondelete="RESTRICT",
            name="fk_scheduled_jobs_account_id",
        ),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_conversations.id",
            ondelete="RESTRICT",
            name="fk_scheduled_jobs_conversation_id",
        ),
        nullable=False,
    )
    name: Mapped[str | None] = mapped_column(String(255))
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    # when 原串 + 解析产物（cron_expr/interval_seconds/advance_minutes）。
    schedule_spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    prompt: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    delivery_channel: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_target: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # 下一未来 occurrence（one-shot = 唯一一次）；NULL = 无待执行。
    next_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outcome: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ScheduleExecutionModel(Base):
    """一次名义 occurrence 的执行记录（只追加不删，崩溃由恢复扫描收束）。

    `schedule_timezone`/`schedule_revision` 是触发时的快照（§5.9.14「时区/计划
    版本」）——job 之后被改名/换时区，历史执行仍可解释。delivery 引用为
    FK RESTRICT：execution 留痕 SHALL NOT 随 delivery 清理丢失。
    """

    __tablename__ = "schedule_executions"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "scheduled_for",
            name="uq_schedule_executions_job_scheduled",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'missed', 'skipped')",
            name="ck_schedule_executions_status",
        ),
        Index("ix_schedule_executions_status_time", "status", "scheduled_for"),
        Index("ix_schedule_executions_tenant", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "scheduled_jobs.id",
            ondelete="RESTRICT",
            name="fk_schedule_executions_job_id",
        ),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skip_reason: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    schedule_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "outbound_delivery_intents.id",
            ondelete="RESTRICT",
            name="fk_schedule_executions_delivery_intent_id",
        ),
    )
    delivery_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_messages.id",
            ondelete="RESTRICT",
            name="fk_schedule_executions_delivery_message_id",
        ),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "SCHEDULE_EXECUTION_STATUSES",
    "SCHEDULE_JOB_STATUSES",
    "SCHEDULE_SKIP_REASONS",
    "ScheduleExecutionModel",
    "ScheduledJobModel",
]
