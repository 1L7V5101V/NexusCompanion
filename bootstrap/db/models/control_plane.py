"""Durable control plane 模型（C2）。

约束命名冻结于 openspec/changes/c2-durable-control-plane/design.md ADR-8；
三事务边界（入站接受 / 执行完成 / 独立 delivery ack）冻结于同文档 §1 与
PILOT_ROADMAP §5.9.11。入站幂等键：Telegram 侧 `(source_channel,
source_identity_id, source_message_id)`、WebChat 侧 `(account_id,
client_message_id)`——两类键同一行只持有一类，唯一性由**部分唯一索引**强制
（PG 对含 NULL 的普通唯一索引不生效）。delivery 状态机
`pending/attempting/sent/failed/dead_letter`：`sent` 只能由 provider ack 推进。
本模块不承担旧单体 `channel:chat_id` 数据的导入或回退（§10 DECIDED）。
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

INBOX_STATUSES = ("accepted", "processed")
"""inbox 收束枚举：processed = durable acceptance/terminal processing 已收束
（`complete_inbound` 等价语义），不表示 channel 已成功展示。"""

TURN_STATUSES = ("queued", "in_progress", "completed", "interrupted", "failed", "cancelled")
"""turn 状态枚举：沿用 ConversationRuntime 冻结终态集（PILOT_ROADMAP §3.1）。"""

TOOL_CALL_STATUSES = ("running", "succeeded", "failed", "cancelled", "unknown")
"""tool call 终态枚举（§5.9.6：active tool 最终都有 terminal/unknown 状态）。"""

WORK_ITEM_STATUSES = ("queued", "in_progress", "succeeded", "failed", "cancelled")
"""后台工作项状态枚举。"""

DELIVERY_INTENT_STATUSES = ("pending", "attempting", "sent", "failed", "dead_letter")
"""delivery 状态机枚举（§5.9.11）；dead_letter 仅管理员 redrive 可回到 pending。"""

DELIVERY_ATTEMPT_OUTCOMES = ("sent", "failed", "redrive")
"""attempt 记录结果枚举：redrive 是管理员处置追加行（ADR-6），非真实投递。"""


class MessageDeduplicationKeyModel(Base):
    """入站幂等双键（§5.9.9）：每行恰好持有一类键（CHECK 强制）。

    Telegram 侧三列组合 / WebChat 侧两列组合各由部分唯一索引强制唯一；
    键插入冲突即「重复注入」，由 repository 回查返回既有身份（ADR-3）。
    """

    __tablename__ = "message_deduplication_keys"
    __table_args__ = (
        CheckConstraint(
            "("
            "(source_message_id IS NOT NULL AND source_channel IS NOT NULL "
            "AND source_identity_id IS NOT NULL)"
            " OR (client_message_id IS NOT NULL AND account_id IS NOT NULL)"
            ")",
            name="ck_message_dedup_keys_key_present",
        ),
        Index(
            "uq_message_dedup_keys_source",
            "source_channel",
            "source_identity_id",
            "source_message_id",
            unique=True,
            postgresql_where=text("source_message_id IS NOT NULL"),
        ),
        Index(
            "uq_message_dedup_keys_client",
            "account_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel: Mapped[str | None] = mapped_column(String(64))
    source_identity_id: Mapped[str | None] = mapped_column(String(255))
    source_message_id: Mapped[str | None] = mapped_column(String(255))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "test_accounts.id",
            ondelete="RESTRICT",
            name="fk_message_dedup_keys_account_id",
        ),
    )
    client_message_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InboxRecordModel(Base):
    """durable inbox：入站接受事务（T1）的收束记录，1:1 绑定去重键。

    status 只表达「接受/处理收束」，与 outbound delivery 状态无关
    （「模型完成 ≠ 已送达」可观测分离，§5.9.11）。
    """

    __tablename__ = "inbox_records"
    __table_args__ = (
        UniqueConstraint("dedup_key_id", name="uq_inbox_records_dedup_key_id"),
        CheckConstraint("status IN ('accepted', 'processed')", name="ck_inbox_records_status"),
        Index(
            "ix_inbox_records_conversation",
            "tenant_id",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_conversations.id",
            ondelete="RESTRICT",
            name="fk_inbox_records_conversation_id",
        ),
        nullable=False,
    )
    dedup_key_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "message_deduplication_keys.id",
            ondelete="RESTRICT",
            name="fk_inbox_records_dedup_key_id",
        ),
        nullable=False,
    )
    canonical_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_messages.id",
            ondelete="RESTRICT",
            name="fk_inbox_records_canonical_message_id",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TurnModel(Base):
    """turn durable 副本（control plane）：状态集与 CAS 语义沿用 ConversationRuntime。

    `inbox_record_id` 唯一 nullable（queued turn 锚到接受事务；proactive/system
    turn 无 inbox）；`final_message_id` 由执行完成事务（T2）写入。
    """

    __tablename__ = "turns"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'in_progress', 'completed', 'interrupted', 'failed', 'cancelled')",
            name="ck_turns_status",
        ),
        Index(
            "uq_turns_inbox_record_id",
            "inbox_record_id",
            unique=True,
            postgresql_where=text("inbox_record_id IS NOT NULL"),
        ),
        Index("ix_turns_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_conversations.id",
            ondelete="RESTRICT",
            name="fk_turns_conversation_id",
        ),
        nullable=False,
    )
    inbox_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "inbox_records.id",
            ondelete="RESTRICT",
            name="fk_turns_inbox_record_id",
        ),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    error_json: Mapped[str | None] = mapped_column(Text)
    final_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_messages.id",
            ondelete="RESTRICT",
            name="fk_turns_final_message_id",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ToolCallModel(Base):
    """tool call 终态流（最小集）：audit/idempotency 键表归 C7，不在本表。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled', 'unknown')",
            name="ck_tool_calls_status",
        ),
        Index("ix_tool_calls_turn", "turn_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "turns.id",
            ondelete="RESTRICT",
            name="fk_tool_calls_turn_id",
        ),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    outcome_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BackgroundWorkItemModel(Base):
    """后台工作项（consolidation 等以 work id 归 C12）；幂等 work 重算归 C3/C12 调度。"""

    __tablename__ = "background_work_items"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_background_work_items_idempotency_key"
        ),
        CheckConstraint(
            "status IN ('queued', 'in_progress', 'succeeded', 'failed', 'cancelled')",
            name="ck_background_work_items_status",
        ),
        Index(
            "ix_background_work_items_tenant_status",
            "tenant_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_conversations.id",
            ondelete="RESTRICT",
            name="fk_background_work_items_conversation_id",
        ),
    )
    work_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundDeliveryIntentModel(Base):
    """outbox intent：执行完成事务（T2）随 final message 原子创建。

    `attempt_count` 是「本 redrive 周期」的尝试计数（redrive 复位）；全量审计流在
    `delivery_attempts`（只追加，redrive 不删不改，ADR-6）。lease 字段供 delivery
    worker 认领/接管（lease_ttl=60s、heartbeat=20s 为冻结初始值，可配置）。
    """

    __tablename__ = "outbound_delivery_intents"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_outbound_delivery_intents_idempotency_key"
        ),
        CheckConstraint(
            "status IN ('pending', 'attempting', 'sent', 'failed', 'dead_letter')",
            name="ck_outbound_delivery_intents_status",
        ),
        Index(
            "ix_outbound_delivery_intents_claim",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_conversations.id",
            ondelete="RESTRICT",
            name="fk_outbound_delivery_intents_conversation_id",
        ),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_messages.id",
            ondelete="RESTRICT",
            name="fk_outbound_delivery_intents_message_id",
        ),
        nullable=False,
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "turns.id",
            ondelete="RESTRICT",
            name="fk_outbound_delivery_intents_turn_id",
        ),
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    target_chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class DeliveryAttemptModel(Base):
    """delivery attempt 审计流（只追加）：sent/failed 为真实投递结果，
    redrive 为管理员处置记录（error 列存原因，ADR-6）。"""

    __tablename__ = "delivery_attempts"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('sent', 'failed', 'redrive')",
            name="ck_delivery_attempts_outcome",
        ),
        Index("ix_delivery_attempts_intent", "intent_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    intent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "outbound_delivery_intents.id",
            ondelete="RESTRICT",
            name="fk_delivery_attempts_intent_id",
        ),
        nullable=False,
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_receipt: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "DELIVERY_ATTEMPT_OUTCOMES",
    "DELIVERY_INTENT_STATUSES",
    "INBOX_STATUSES",
    "TOOL_CALL_STATUSES",
    "TURN_STATUSES",
    "WORK_ITEM_STATUSES",
    "BackgroundWorkItemModel",
    "DeliveryAttemptModel",
    "InboxRecordModel",
    "MessageDeduplicationKeyModel",
    "OutboundDeliveryIntentModel",
    "ToolCallModel",
    "TurnModel",
]
