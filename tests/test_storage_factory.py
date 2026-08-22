"""存储工厂测试（M4）。postgres 相关用例依赖本地 PG，无 PG 自动 skip。"""
import os
import uuid

import psycopg
import pytest

from agent.config_models import StorageConfig
from infra.storage.factory import create_session_store, create_store
from infra.storage.postgres_memory_store import PostgresMemoryStore
from infra.storage.postgres_session_store import PostgresSessionStore
from memory2.store import MemoryStore2
from session.store import SessionStore

PG_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture
def pg_url() -> str:
    if not _pg_alive(PG_URL):
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 postgres 工厂测试")
    return PG_URL


def _sqlite_cfg() -> StorageConfig:
    return StorageConfig(backend="sqlite")


def _pg_cfg(url: str) -> StorageConfig:
    return StorageConfig(backend="postgres", postgres_url=url)


def _swap_db(url: str, dbname: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + dbname


def test_create_store_sqlite(tmp_path) -> None:
    store = create_store(_sqlite_cfg(), tmp_path / "m.db", vec_dim=64)
    assert isinstance(store, MemoryStore2)
    store.close()


def test_create_session_store_sqlite(tmp_path) -> None:
    store = create_session_store(_sqlite_cfg(), tmp_path / "s.db")
    assert isinstance(store, SessionStore)
    store.close()


def test_create_store_unknown_backend(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unknown backend"):
        create_store(StorageConfig(backend="mysql"), tmp_path / "m.db")


def test_create_session_store_unknown_backend(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unknown backend"):
        create_session_store(StorageConfig(backend="mysql"), tmp_path / "s.db")


@pytest.mark.postgres
def test_create_store_postgres(pg_url: str, tmp_path) -> None:
    store = create_store(_pg_cfg(pg_url), tmp_path / "m.db", vec_dim=64)
    assert isinstance(store, PostgresMemoryStore)
    store.close()


@pytest.mark.postgres
def test_create_session_store_postgres(pg_url: str, tmp_path) -> None:
    store = create_session_store(_pg_cfg(pg_url), tmp_path / "s.db")
    assert isinstance(store, PostgresSessionStore)
    store.close()


@pytest.mark.postgres
def test_create_store_probe_missing_schema(pg_url: str) -> None:
    """指向未跑 migration 的空库，断言工厂抛含 alembic upgrade head 的错误。"""
    dbname = f"nexus_probe_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    admin = psycopg.connect(pg_url, autocommit=True)
    admin.execute(f"CREATE DATABASE {dbname}")
    admin.close()
    scratch_url = _swap_db(pg_url, dbname)
    try:
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            create_store(_pg_cfg(scratch_url), "unused.db")
    finally:
        admin = psycopg.connect(pg_url, autocommit=True)
        admin.execute(f"DROP DATABASE IF EXISTS {dbname}")
        admin.close()
