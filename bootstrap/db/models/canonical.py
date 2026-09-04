"""Canonical identity 模型（C1）：account → tenant → canonical conversation。

约束命名冻结于 openspec/changes/2026-09-05-c1-canonical-identity/design.md ADR-7；
语义门禁为 PILOT_ROADMAP §5.9.2 / §5.9.9：一个 test_account 对应一个 tenant，
一个 tenant 对应一个 canonical_conversation，canonical message 在
(conversation_id, sequence) 唯一且 sequence 为 per-conversation 0-based BIGINT。
本模块不承担旧单体 `channel:chat_id` 数据的导入或映射（§10 DECIDED）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base

ACCOUNT_STATUSES = ("provisioning", "active", "suspended", "revoked")
"""账号状态枚举（§5.9.9 冻结）：revoked 为终态，suspended 可解除但旧凭据不复活。"""

CONVERSATION_STATUSES = ("active", "archived")
"""规范会话状态枚举：C1 只区分可用与归档，archived 不做任何行为。"""

MESSAGE_ROLES = ("user", "assistant", "system", "tool")
"""canonical message 角色枚举。"""


class TestAccountModel(Base):
    """受邀测试账号（登录主体）。auth token/session 表归 C5，不在本表。"""

    __tablename__ = "test_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_test_accounts_tenant_id"),
        CheckConstraint(
            "status IN ('provisioning', 'active', 'suspended', 'revoked')",
            name="ck_test_accounts_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    # 资源边界：账号与 tenant 严格 1:1（§5.9.2）。
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="provisioning"
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CanonicalConversationModel(Base):
    """规范会话：每个 tenant 恰有一个；channel binding 只负责映射到它（C10）。"""

    __tablename__ = "canonical_conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_canonical_conversations_tenant_id"),
        CheckConstraint(
            "status IN ('active', 'archived')", name="ck_canonical_conversations_status"
        ),
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
            name="fk_canonical_conversations_account_id",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # per-conversation 0-based sequence 计数器；取号 = 单事务 UPDATE +1 RETURNING，
    # 禁止「应用内读号加一再单独提交」（§5.9.2）。
    next_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CanonicalMessageModel(Base):
    """canonical message stream：服务端生成 message id，保留 source/client 原文标识。

    入站去重幂等键（inbox/dedupe）归 C2；本表只保存标识原文，不建去重唯一约束。
    """

    __tablename__ = "canonical_messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_canonical_messages_conversation_sequence",
        ),
        Index(
            "ix_canonical_messages_tenant_conversation",
            "tenant_id",
            "conversation_id",
            "sequence",
        ),
        CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="ck_canonical_messages_role",
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
            name="fk_canonical_messages_conversation_id",
        ),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    source_channel: Mapped[str | None] = mapped_column(String(64))
    source_identity_id: Mapped[str | None] = mapped_column(String(255))
    source_message_id: Mapped[str | None] = mapped_column(String(255))
    client_message_id: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "ACCOUNT_STATUSES",
    "CONVERSATION_STATUSES",
    "MESSAGE_ROLES",
    "CanonicalConversationModel",
    "CanonicalMessageModel",
    "TestAccountModel",
]
