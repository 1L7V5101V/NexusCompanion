"""Telegram binding 模型（C10：telegram-binding-sync）。

约束命名冻结于 openspec/changes/c10-telegram-binding-sync/design.md ADR-1：
active 绑定在 account 侧与 platform identity 侧由**部分唯一索引**双重唯一
（§5.9.2），unbound 行保留供审计且不阻塞重绑；绑定码 digest-only（沿用
access_tokens 约定，明文不落库不进审计）。本模块只承载绑定语义，canonical
message schema（C1）与 WebChat 协议（C4）零改动。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base

DIGEST_LENGTH = 64
"""HMAC/SHA-256 hex digest 固定长度（与 access_tokens CHECK 同款）。"""

BINDING_STATUSES = ("active", "unbound")
"""绑定状态：unbound 为保留审计的终态（可重绑产生新行）。"""

BINDING_VIAS = ("admin", "code")
"""绑定来源：管理员预绑定 / 一次性绑定码兑换。"""


class TelegramIdentityBindingModel(Base):
    """Telegram 私聊身份 ↔ test_account 的 active/unbound 绑定行。

    双重唯一（部分唯一索引，迁移内定义）：``uq_tib_account_active`` 与
    ``uq_tib_identity_active`` 只约束 ``status='active'`` 行——解绑行不阻塞
    同身份/同账号重绑。``tenant_id`` 是绑定时点冻结的目标 agent（账号
    created_at 首个 canonical 会话）。
    """

    __tablename__ = "telegram_identity_bindings"
    __table_args__ = (
        Index(
            "uq_tib_account_active",
            "account_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_tib_identity_active",
            "telegram_user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint(
            "status IN ('active', 'unbound')", name="ck_tib_status"
        ),
        CheckConstraint(
            "bound_via IN ('admin', 'code')", name="ck_tib_bound_via"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "test_accounts.id",
            ondelete="RESTRICT",
            name="fk_tib_account_id",
        ),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    bound_via: Mapped[str] = mapped_column(String(16), nullable=False)
    bound_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    unbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unbound_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TelegramBindingCodeModel(Base):
    """一次性绑定码（digest-only）：10 分钟过期、单次使用、与账号预关联。

    兑换原子性（行锁 + 条件消费 + 绑定插入同事务）见
    ``TelegramBindingService.redeem``；``consumed_at`` 非 NULL 即已消费。
    """

    __tablename__ = "telegram_binding_codes"
    __table_args__ = (
        UniqueConstraint("code_digest", name="uq_tbc_digest"),
        CheckConstraint(
            f"char_length(code_digest) = {DIGEST_LENGTH}",
            name="ck_tbc_digest_sha256",
        ),
        Index(
            "ix_tbc_account_open",
            "account_id",
            "created_at",
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "test_accounts.id",
            ondelete="RESTRICT",
            name="fk_tbc_account_id",
        ),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    issued_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "BINDING_STATUSES",
    "BINDING_VIAS",
    "DIGEST_LENGTH",
    "TelegramBindingCodeModel",
    "TelegramIdentityBindingModel",
]
