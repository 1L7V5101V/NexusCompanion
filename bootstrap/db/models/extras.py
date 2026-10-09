from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base, TenantMixin, TimestampMixin


class AppConfigModel(Base, TenantMixin, TimestampMixin):
    """Generic key-value configuration for arbitrary app settings.

    Stores things like MCP server configs, proactive sources, etc.
    as JSON blobs keyed by a logical name.
    """

    __tablename__ = "app_configs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)


# 旧 ScheduledJobModel（d6e1cd9205cd，String PK + channel/chat_id 模型）已随 C11
# 移除：其表由 e8b4c2a6d9f1 重命名为 scheduled_jobs_import_legacy（一次性导入
# 脚本的派生拷贝，无运行时读取方），规范 schedule 存储见 bootstrap/db/models/
# schedule.py（c11-explicit-schedules design ADR-1/ADR-7）。
