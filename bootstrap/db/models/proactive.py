from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base, TenantMixin


class DeliveryModel(Base, TenantMixin):
    """Record of a proactive delivery sent to a session."""

    __tablename__ = "deliveries"

    session_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    delivery_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionStateModel(Base, TenantMixin):
    """Key-value state storage per session."""

    __tablename__ = "session_state"

    session_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class ContextOnlyTimestampModel(Base, TenantMixin):
    """Log of context-only (no delivery) proactive ticks."""

    __tablename__ = "context_only_timestamps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(String(255), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TickLogModel(Base, TenantMixin):
    """A proactive tick execution log."""

    __tablename__ = "tick_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tick_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    session_key: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gate_exit: Mapped[str | None] = mapped_column(String(64))
    terminal_action: Mapped[str | None] = mapped_column(String(64))
    skip_reason: Mapped[str | None] = mapped_column(Text)
    steps_taken: Mapped[int | None] = mapped_column(Integer)
    alert_count: Mapped[int | None] = mapped_column(Integer)
    content_count: Mapped[int | None] = mapped_column(Integer)
    context_count: Mapped[int | None] = mapped_column(Integer)
    interesting_ids: Mapped[str | None] = mapped_column(Text)
    discarded_ids: Mapped[str | None] = mapped_column(Text)
    cited_ids: Mapped[str | None] = mapped_column(Text)
    drift_entered: Mapped[bool | None] = mapped_column(Integer)
    final_message: Mapped[str | None] = mapped_column(Text)
    proactive_effects_json: Mapped[str | None] = mapped_column(Text)


class TickStepLogModel(Base, TenantMixin):
    """Individual step within a proactive tick."""

    __tablename__ = "tick_step_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tick_id: Mapped[str] = mapped_column(String(64), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str | None] = mapped_column(String(64))
    tool_name: Mapped[str | None] = mapped_column(String(128))
    tool_call_id: Mapped[str | None] = mapped_column(String(128))
    tool_args_json: Mapped[str | None] = mapped_column(Text)
    tool_result_text: Mapped[str | None] = mapped_column(Text)
    terminal_action_after: Mapped[str | None] = mapped_column(String(64))
    skip_reason_after: Mapped[str | None] = mapped_column(Text)
    interesting_ids_after: Mapped[str | None] = mapped_column(Text)
    discarded_ids_after: Mapped[str | None] = mapped_column(Text)
    cited_ids_after: Mapped[str | None] = mapped_column(Text)
    final_message_after: Mapped[str | None] = mapped_column(Text)
