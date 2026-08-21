"""存储层工厂：按 config.backend 返回 SQLite 或 PostgreSQL store（M4）。

沿用 SCALING_PLAN 的 create_store 设计；StorageConfig 无 sqlite_path 字段，
故 sqlite 后端路径由调用方（workspace 相关）显式传入。tenant_id 默认 "default"
（决策 C：单库多租户，Phase 1 单租户）。
"""

from pathlib import Path

import psycopg

from agent.config_models import StorageConfig
from infra.storage.interfaces import MemoryStorage, SessionStorage
from infra.storage.postgres_memory_store import PostgresMemoryStore
from infra.storage.postgres_session_store import PostgresSessionStore
from infra.storage.runtime import StorageRuntime
from memory2.store import VEC_DIM, MemoryStore2
from session.store import SessionStore


def _check_pg_schema(postgres_url: str, table: str) -> None:
    """探测目标表是否已建。schema 由 alembic 管理（store 构造不建表），
    缺失时提前报错，避免失败推迟到首次真实查询才暴露。"""
    url = postgres_url
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(url)
    try:
        row = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        raise RuntimeError(
            f"PostgreSQL schema 未初始化（缺少表 {table}），请先执行：alembic upgrade head"
        )


def create_store(
    config: StorageConfig,
    sqlite_path: str | Path,
    *,
    tenant_id: str = "default",
    vec_dim: int = VEC_DIM,
) -> MemoryStorage:
    """按 backend 创建记忆 store，返回共同接口 MemoryStorage。"""
    if config.backend == "sqlite":
        return MemoryStore2(sqlite_path, vec_dim=vec_dim)
    if config.backend == "postgres":
        _check_pg_schema(config.postgres_url, "memory_items")
        return PostgresMemoryStore(
            config.postgres_url, tenant_id=tenant_id, vec_dim=vec_dim
        )
    raise ValueError(f"Unknown backend: {config.backend}")


def create_session_store(
    config: StorageConfig,
    sqlite_path: str | Path,
    *,
    tenant_id: str = "default",
) -> SessionStorage:
    """按 backend 创建 session store，返回共同接口 SessionStorage。"""
    if config.backend == "sqlite":
        return SessionStore(sqlite_path)
    if config.backend == "postgres":
        _check_pg_schema(config.postgres_url, "sessions")
        return PostgresSessionStore(config.postgres_url, tenant_id=tenant_id)
    raise ValueError(f"Unknown backend: {config.backend}")


def create_storage_runtime(
    config: StorageConfig,
    memory_path: str | Path,
    sessions_path: str | Path,
    *,
    vec_dim: int = VEC_DIM,
) -> StorageRuntime:
    """生产入口：进程级 StorageRuntime（bootstrap 创建一次）。

    与 create_store/create_session_store 的关系：后者每调用开一条连接，仅限
    测试 / 显式 single-store 调用方；生产统一走 runtime.for_tenant(ctx) 取
    tenant-bound view，由 runtime 持有 backend 连接并负责关闭。
    """
    if config.backend == "postgres":
        _check_pg_schema(config.postgres_url, "memory_items")
        _check_pg_schema(config.postgres_url, "sessions")
        return StorageRuntime(
            config.postgres_url, memory_path, sessions_path, vec_dim=vec_dim
        )
    if config.backend == "sqlite":
        return StorageRuntime(None, memory_path, sessions_path, vec_dim=vec_dim)
    raise ValueError(f"Unknown backend: {config.backend}")
