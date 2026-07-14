from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base, TenantMixin, TimestampMixin


class ScheduledJobModel(Base, TenantMixin, TimestampMixin):
    """Persistent scheduled jobs for the APScheduler."""

    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)  # at / after / every
    tier: Mapped[str] = mapped_column(String(32), nullable=False)  # instant / soft
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    cron_expr: Mapped[str | None] = mapped_column(String(128))
    message: Mapped[str | None] = mapped_column(Text)
    prompt: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AppConfigModel(Base, TenantMixin, TimestampMixin):
    """Generic key-value configuration for arbitrary app settings.

    Stores things like MCP server configs, proactive sources, etc.
    as JSON blobs keyed by a logical name.
    """

    __tablename__ = "app_configs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
