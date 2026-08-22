from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base, TenantMixin, TimestampMixin


class RachaelNodeModel(Base, TenantMixin):
    """A node in the Rachael associative memory graph."""

    __tablename__ = "rachael_nodes"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    anchor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_key: Mapped[str] = mapped_column(String(255), nullable=False)
    turn_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    first_ts_unix: Mapped[float] = mapped_column(Float, nullable=False)
    salience: Mapped[float] = mapped_column(Float, nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False)
    resource: Mapped[float] = mapped_column(Float, nullable=False)
    recall_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_activated_ts: Mapped[float] = mapped_column(Float, nullable=False)
    last_strength_ts: Mapped[float] = mapped_column(Float, nullable=False)
    last_resource_ts: Mapped[float] = mapped_column(Float, nullable=False)
    embedding: Mapped[str | None] = mapped_column(Text)
    emb_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class RachaelEdgeModel(Base, TenantMixin):
    """A weighted edge between two nodes in the memory graph."""

    __tablename__ = "rachael_edges"

    src_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    dst_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    co_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_used_ts: Mapped[float] = mapped_column(Float, nullable=False)


class RachaelQueryLogModel(Base, TenantMixin):
    """Log of memory queries and their results."""

    __tablename__ = "rachael_query_log"

    query_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_key: Mapped[str] = mapped_column(String(255), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[str] = mapped_column(String(64), nullable=False)
    seed_count: Mapped[int] = mapped_column(Integer, default=0)
    pool_count: Mapped[int] = mapped_column(Integer, default=0)
    activated_count: Mapped[int] = mapped_column(Integer, default=0)
    activation_threshold: Mapped[float] = mapped_column(Float, default=0)
    dense_count: Mapped[int] = mapped_column(Integer, default=0)
    ripple_count: Mapped[int] = mapped_column(Integer, default=0)
    inject_chars: Mapped[int] = mapped_column(Integer, default=0)
    source_ref_count: Mapped[int] = mapped_column(Integer, default=0)
    activation_items: Mapped[str | None] = mapped_column(Text)
    dense_items: Mapped[str | None] = mapped_column(Text)
    ripple_items: Mapped[str | None] = mapped_column(Text)
    text_block_preview: Mapped[str | None] = mapped_column(Text)


class RachaelActivationEventModel(Base, TenantMixin):
    """Individual activation events during memory retrieval."""

    __tablename__ = "rachael_activation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    query_id: Mapped[str] = mapped_column(String(64), nullable=False)
    activated_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    direct_score: Mapped[float] = mapped_column(Float, nullable=False)
    state_score: Mapped[float] = mapped_column(Float, nullable=False)
    edge_score: Mapped[float] = mapped_column(Float, nullable=False)
    long_score: Mapped[float] = mapped_column(Float, nullable=False)
    resource: Mapped[float] = mapped_column(Float, nullable=False)
    fan: Mapped[int] = mapped_column(Integer, nullable=False)


class RachaelEmbeddingCacheModel(Base, TenantMixin):
    """Cache of computed embeddings to avoid redundant API calls."""

    __tablename__ = "rachael_embedding_cache"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[str | None] = mapped_column(Text)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class RachaelSalienceStateModel(Base, TenantMixin):
    """Running salience state for graph nodes."""

    __tablename__ = "rachael_salience_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    vector_sum: Mapped[str | None] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class RachaelMigrationRunModel(Base, TenantMixin):
    """Track embedding migration runs."""

    __tablename__ = "rachael_migration_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_db_path: Mapped[str] = mapped_column(String(512), nullable=False)
    target_db_path: Mapped[str] = mapped_column(String(512), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[str] = mapped_column(String(64), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    activation_count: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit_count: Mapped[int] = mapped_column(Integer, default=0)
    cache_miss_count: Mapped[int] = mapped_column(Integer, default=0)


class RachaelSourceSessionSnapshotModel(Base, TenantMixin):
    """Per-session migration cursor."""

    __tablename__ = "rachael_source_session_snapshot"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_consolidated: Mapped[int] = mapped_column(Integer, nullable=False)
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    max_seq: Mapped[int] = mapped_column(Integer, nullable=False)
