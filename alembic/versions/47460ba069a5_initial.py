"""initial schema

Creates all core tables for the Akashic Agent multi-tenant database.

Revision ID: 47460ba069a5
Revises: None
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "47460ba069a5"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── tenants ──────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(128), unique=True, nullable=False),
        sa.Column("plan", sa.String(32), server_default="starter"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ── sessions ─────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("channel", sa.String(64), nullable=False, server_default=""),
        sa.Column("chat_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("last_consolidated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_user_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_proactive_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_sessions_tenant_updated", "sessions", ["tenant_id", "updated_at"])

    # ── messages ─────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_key", sa.String(255), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_chain", sa.Text(), nullable=True),
        sa.Column("extra", sa.Text(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_messages_tenant_session",
        "messages",
        ["tenant_id", "session_key", "seq"],
    )

    # ── memory_items ─────────────────────────────────────────
    op.create_table(
        "memory_items",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("memory_type", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("reinforcement", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("emotional_weight", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extra_json", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.String(255), nullable=True),
        sa.Column("happened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_memory_items_content_hash",
        "memory_items",
        ["tenant_id", "content_hash", "memory_type"],
        unique=True,
    )
    op.create_index(
        "ix_memory_items_type_status",
        "memory_items",
        ["tenant_id", "memory_type", "status"],
    )

    # ── consolidation_events ─────────────────────────────────
    op.create_table(
        "consolidation_events",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("source_ref", sa.String(255), primary_key=True),
        sa.Column("item_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── memory_replacements ──────────────────────────────────
    op.create_table(
        "memory_replacements",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("old_item_id", sa.String(64), nullable=False),
        sa.Column("old_memory_type", sa.String(64), nullable=False),
        sa.Column("old_summary", sa.Text(), nullable=False),
        sa.Column("old_source_ref", sa.String(255), nullable=True),
        sa.Column("old_happened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("old_extra_json", sa.Text(), nullable=True),
        sa.Column("new_item_id", sa.String(64), nullable=False),
        sa.Column("new_memory_type", sa.String(64), nullable=False),
        sa.Column("new_summary", sa.Text(), nullable=False),
        sa.Column("new_source_ref", sa.String(255), nullable=True),
        sa.Column("new_happened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_extra_json", sa.Text(), nullable=True),
        sa.Column("relation_type", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── deliveries ───────────────────────────────────────────
    op.create_table(
        "deliveries",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("session_key", sa.String(255), primary_key=True),
        sa.Column("delivery_key", sa.String(255), primary_key=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_deliveries_tenant_session",
        "deliveries",
        ["tenant_id", "session_key", "sent_at"],
    )

    # ── session_state ────────────────────────────────────────
    op.create_table(
        "session_state",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("session_key", sa.String(255), primary_key=True),
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
    )

    # ── context_only_timestamps ──────────────────────────────
    op.create_table(
        "context_only_timestamps",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_key", sa.String(255), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_context_only_tenant_session",
        "context_only_timestamps",
        ["tenant_id", "session_key", "ts"],
    )

    # ── tick_log ─────────────────────────────────────────────
    op.create_table(
        "tick_log",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tick_id", sa.String(64), unique=True, nullable=False),
        sa.Column("session_key", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gate_exit", sa.String(64), nullable=True),
        sa.Column("terminal_action", sa.String(64), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("steps_taken", sa.Integer(), nullable=True),
        sa.Column("alert_count", sa.Integer(), nullable=True),
        sa.Column("content_count", sa.Integer(), nullable=True),
        sa.Column("context_count", sa.Integer(), nullable=True),
        sa.Column("interesting_ids", sa.Text(), nullable=True),
        sa.Column("discarded_ids", sa.Text(), nullable=True),
        sa.Column("cited_ids", sa.Text(), nullable=True),
        sa.Column("drift_entered", sa.Integer(), nullable=True),
        sa.Column("final_message", sa.Text(), nullable=True),
        sa.Column("proactive_effects_json", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_tick_log_tenant_session",
        "tick_log",
        ["tenant_id", "session_key", "started_at"],
    )

    # ── tick_step_log ────────────────────────────────────────
    op.create_table(
        "tick_step_log",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tick_id", sa.String(64), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(64), nullable=True),
        sa.Column("tool_name", sa.String(128), nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=True),
        sa.Column("tool_args_json", sa.Text(), nullable=True),
        sa.Column("tool_result_text", sa.Text(), nullable=True),
        sa.Column("terminal_action_after", sa.String(64), nullable=True),
        sa.Column("skip_reason_after", sa.Text(), nullable=True),
        sa.Column("interesting_ids_after", sa.Text(), nullable=True),
        sa.Column("discarded_ids_after", sa.Text(), nullable=True),
        sa.Column("cited_ids_after", sa.Text(), nullable=True),
        sa.Column("final_message_after", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_tick_step_log_tick",
        "tick_step_log",
        ["tick_id", "step_index"],
    )


def downgrade() -> None:
    op.drop_table("tick_step_log")
    op.drop_table("tick_log")
    op.drop_table("context_only_timestamps")
    op.drop_table("session_state")
    op.drop_table("deliveries")
    op.drop_table("memory_replacements")
    op.drop_table("consolidation_events")
    op.drop_table("memory_items")
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("tenants")
