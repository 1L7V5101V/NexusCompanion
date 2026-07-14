from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base, TimestampMixin


class TenantModel(Base, TimestampMixin):
    """A tenant (organisation / user group) that owns isolated data."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(32), default="starter")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[str | None] = mapped_column(Text)

    # override TimestampMixin.updated_at to make it non-nullable for tenant rows
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
