"""Attachment metadata 模型（C6，openspec/changes/c6-attachment/design.md ADR-4）。

``attachments`` 承载 §5.9.15 的 Attachment 语义：immutable ``id``（= attachment_id，
客户端可见）、owner（account+tenant 双维度）、size/detected MIME/checksum/storage
key/status/引用/retention deadline。``message_attachments`` 是多对多关联表：一条
canonical message 可引用多个附件、一个附件可被多条消息引用（转发，更新
last_referenced_at）。status 枚举 {staged, committed, missing} 与 migration
CHECK 冻结一致；storage_key 为相对 blob root 的服务器派生路径。
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

ATTACHMENT_STATUSES = ("staged", "committed", "missing")
"""附件状态枚举（§5.9.15 + C6 ADR-4）：staged = 上传中（staging 区，24h 清理）；
committed = 已 rename + metadata 同事务提交；missing = metadata 在但 blob 缺失
（reconciliation 标记，读取返回 attachment_blob_missing）。"""


class AttachmentModel(Base):
    """附件 metadata（blob 本体在 tenant 命名空间磁盘，本表是 canonical 元数据）。"""

    __tablename__ = "attachments"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_attachments_storage_key"),
        CheckConstraint(
            "status IN ('staged', 'committed', 'missing')",
            name="ck_attachments_status",
        ),
        CheckConstraint(
            "referencing_count >= 0", name="ck_attachments_referencing_count"
        ),
        Index("ix_attachments_tenant_status", "tenant_id", "status"),
        Index("ix_attachments_retention_deadline", "retention_deadline"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "test_accounts.id", ondelete="RESTRICT", name="fk_attachments_account_id"
        ),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    detected_mime: Mapped[str] = mapped_column(String(64), nullable=False)
    server_ext: Mapped[str] = mapped_column(String(16), nullable=False)
    # 客户端原始文件名（仅展示）：服务端清洗 + redaction 面，不参与路径。
    filename_display: Mapped[str] = mapped_column(String(255), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    # 相对 blob root 的路径：{tenant_dirname}/{attachment_id}.{server_ext}
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="staged"
    )
    referencing_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_referenced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    retention_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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


class MessageAttachmentModel(Base):
    """message ↔ attachment 引用（多对多；转发 = 在同一附件上追加引用）。"""

    __tablename__ = "message_attachments"
    __table_args__ = (
        Index("ix_message_attachments_attachment", "attachment_id"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "canonical_messages.id",
            ondelete="CASCADE",
            name="fk_message_attachments_message_id",
        ),
        primary_key=True,
    )
    attachment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "attachments.id",
            ondelete="CASCADE",
            name="fk_message_attachments_attachment_id",
        ),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "ATTACHMENT_STATUSES",
    "AttachmentModel",
    "MessageAttachmentModel",
]