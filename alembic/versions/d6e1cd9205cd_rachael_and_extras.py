"""rachael graph engine tables + app configs

Adds tables for:
  - Rachael associative memory graph (rachael_nodes, rachael_edges, ...)
  - Scheduled jobs (scheduled_jobs)
  - Generic app config store (app_configs)

Revision ID: d6e1cd9205cd
Revises: 47460ba069a5
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d6e1cd9205cd"
down_revision: Union[str, Sequence[str], None] = "47460ba069a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── rachael_nodes ─────────────────────────────────────────
    op.create_table(
        "rachael_nodes",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("anchor_id", sa.String(128), nullable=False),
        sa.Column("session_key", sa.String(255), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("first_ts_unix", sa.Float(), nullable=False),
        sa.Column("salience", sa.Float(), nullable=False),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("resource", sa.Float(), nullable=False),
        sa.Column("recall_count", sa.Integer(), nullable=False),
        sa.Column("last_activated_ts", sa.Float(), nullable=False),
        sa.Column("last_strength_ts", sa.Float(), nullable=False),
        sa.Column("last_resource_ts", sa.Float(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("emb_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String(64), nullable=False),
    )

    # ── rachael_edges ─────────────────────────────────────────
    op.create_table(
        "rachael_edges",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("src_key", sa.String(128), primary_key=True),
        sa.Column("dst_key", sa.String(128), primary_key=True),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("co_count", sa.Integer(), nullable=False),
        sa.Column("last_used_ts", sa.Float(), nullable=False),
    )

    # ── rachael_query_log ─────────────────────────────────────
    op.create_table(
        "rachael_query_log",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("query_id", sa.String(64), primary_key=True),
        sa.Column("session_key", sa.String(255), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(64), nullable=False),
        sa.Column("ts", sa.String(64), nullable=False),
        sa.Column("seed_count", sa.Integer(), server_default="0"),
        sa.Column("pool_count", sa.Integer(), server_default="0"),
        sa.Column("activated_count", sa.Integer(), server_default="0"),
        sa.Column("activation_threshold", sa.Float(), server_default="0"),
        sa.Column("dense_count", sa.Integer(), server_default="0"),
        sa.Column("ripple_count", sa.Integer(), server_default="0"),
        sa.Column("inject_chars", sa.Integer(), server_default="0"),
        sa.Column("source_ref_count", sa.Integer(), server_default="0"),
        sa.Column("activation_items", sa.Text(), nullable=True),
        sa.Column("dense_items", sa.Text(), nullable=True),
        sa.Column("ripple_items", sa.Text(), nullable=True),
        sa.Column("text_block_preview", sa.Text(), nullable=True),
    )

    # ── rachael_activation_events ─────────────────────────────
    op.create_table(
        "rachael_activation_events",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("query_id", sa.String(64), nullable=False),
        sa.Column("activated_key", sa.String(128), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("direct_score", sa.Float(), nullable=False),
        sa.Column("state_score", sa.Float(), nullable=False),
        sa.Column("edge_score", sa.Float(), nullable=False),
        sa.Column("long_score", sa.Float(), nullable=False),
        sa.Column("resource", sa.Float(), nullable=False),
        sa.Column("fan", sa.Integer(), nullable=False),
    )

    # ── rachael_embedding_cache ───────────────────────────────
    op.create_table(
        "rachael_embedding_cache",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("message_id", sa.String(128), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String(64), nullable=False),
    )

    # ── rachael_salience_state ────────────────────────────────
    op.create_table(
        "rachael_salience_state",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("vector_sum", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(64), nullable=False),
    )

    # ── rachael_migration_runs ────────────────────────────────
    op.create_table(
        "rachael_migration_runs",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("source_db_path", sa.String(512), nullable=False),
        sa.Column("target_db_path", sa.String(512), nullable=False),
        sa.Column("embedding_model", sa.String(128), nullable=False),
        sa.Column("started_at", sa.String(64), nullable=False),
        sa.Column("finished_at", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default="0"),
        sa.Column("activation_count", sa.Integer(), server_default="0"),
        sa.Column("cache_hit_count", sa.Integer(), server_default="0"),
        sa.Column("cache_miss_count", sa.Integer(), server_default="0"),
    )

    # ── rachael_source_session_snapshot ───────────────────────
    op.create_table(
        "rachael_source_session_snapshot",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("session_key", sa.String(255), primary_key=True),
        sa.Column("last_consolidated", sa.Integer(), nullable=False),
        sa.Column("next_seq", sa.Integer(), nullable=False),
        sa.Column("max_seq", sa.Integer(), nullable=False),
    )

    # ── scheduled_jobs ───────────────────────────────────────
    op.create_table(
        "scheduled_jobs",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("tier", sa.String(32), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.String(255), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("cron_expr", sa.String(128), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("timezone", sa.String(64), server_default="UTC"),
        sa.Column("run_count", sa.Integer(), server_default="0"),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true")),
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

    # ── app_configs ──────────────────────────────────────────
    op.create_table(
        "app_configs",
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value_json", sa.Text(), nullable=False),
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


def downgrade() -> None:
    op.drop_table("app_configs")
    op.drop_table("scheduled_jobs")
    op.drop_table("rachael_source_session_snapshot")
    op.drop_table("rachael_migration_runs")
    op.drop_table("rachael_salience_state")
    op.drop_table("rachael_embedding_cache")
    op.drop_table("rachael_activation_events")
    op.drop_table("rachael_query_log")
    op.drop_table("rachael_edges")
    op.drop_table("rachael_nodes")
