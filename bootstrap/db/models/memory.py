from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base, TenantMixin, TimestampMixin


class MemoryItemModel(Base, TenantMixin, TimestampMixin):
    """A single memory item with optional embedding."""

    __tablename__ = "memory_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[str | None] = mapped_column(Text)
    reinforcement: Mapped[int] = mapped_column(Integer, default=1)
    emotional_weight: Mapped[int] = mapped_column(Integer, default=0)
    extra_json: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(String(255))
    happened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="active")


class ConsolidationEventModel(Base, TenantMixin):
    """Tracks consolidation events for memory deduplication."""

    __tablename__ = "consolidation_events"

    source_ref: Mapped[str] = mapped_column(String(255), primary_key=True)
    item_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryReplacementModel(Base, TenantMixin):
    """Log of memory item replacements during consolidation."""

    __tablename__ = "memory_replacements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    old_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    old_memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    old_summary: Mapped[str] = mapped_column(Text, nullable=False)
    old_source_ref: Mapped[str | None] = mapped_column(String(255))
    old_happened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    old_extra_json: Mapped[str | None] = mapped_column(Text)
    new_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    new_memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    new_summary: Mapped[str] = mapped_column(Text, nullable=False)
    new_source_ref: Mapped[str | None] = mapped_column(String(255))
    new_happened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    new_extra_json: Mapped[str | None] = mapped_column(Text)
    relation_type: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
