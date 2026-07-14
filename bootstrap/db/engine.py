from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from bootstrap.db.config import DatabaseConfig


def create_engine(cfg: DatabaseConfig) -> AsyncEngine:
    return create_async_engine(
        cfg.url,
        pool_size=cfg.pool_size,
        max_overflow=cfg.max_overflow,
        echo=cfg.echo,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
