from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base, TenantMixin, TimestampMixin


class SessionModel(Base, TenantMixin, TimestampMixin):
    """A conversation session belonging to a tenant."""

    __tablename__ = "sessions"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    channel: Mapped[str] = mapped_column(String(64), default="")
    chat_id: Mapped[str] = mapped_column(String(255), default="")
    metadata_json: Mapped[str | None] = mapped_column(Text)
    last_consolidated: Mapped[int] = mapped_column(Integer, default=0)
    last_user_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_proactive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_seq: Mapped[int] = mapped_column(Integer, default=0)


class MessageModel(Base, TenantMixin):
    """A single message within a session."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_key: Mapped[str] = mapped_column(String(255), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    tool_chain: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
