"""c18 task 6.4：PG 侧画像存储实现。

会话级独立 scratch 库 `nexus_c18test`（同 `tests/auth_provisioning/conftest.py` 的做法，
不污染共享 `nexus` 库），空库执行 `alembic upgrade head` 拿到 b7d3f5a9c1e4 建的两张表。
本地 PG 不可用时整组 skip。

用例正文在 `tests/memory/profile_store_cases.py`，与 `test_sqlite_profile_store.py` 共用
同一份清单——本文件只负责 PG 特有的那几件事：借会话、提交与中止它、按租户再开一个实例、
跑裸 SQL。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.config import Config

from agent.memory_pg import PgMemoryProfileStore
from scripts.migrate.alembic_util import upgrade_head
from tests.memory.profile_store_cases import CLEANUP_PREFIX, TENANT_A, CASES

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.postgres

ADMIN_URL = "postgresql://nexus:nexus_dev@localhost:5433/nexus"
SCRATCH_DB = "nexus_c18test"
_PROFILE_TABLES = ("tenant_memory_profile_sections", "tenant_memory_profile_entries")


def _pg_alive() -> bool:
    try:
        conn = psycopg.connect(ADMIN_URL, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


def _as_pool_url(url: str, *, psycopg: bool = True) -> str:
    scheme = "postgresql+psycopg://" if psycopg else "postgresql+asyncpg://"
    return url.replace("postgresql://", scheme, 1)


@pytest.fixture(scope="session")
def c18_pg_url() -> Iterator[str]:
    if not _pg_alive():
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过画像存储 PG 集成测试")
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
    conn = psycopg.connect(url, autocommit=True)
    # 迁移链需要 vector/pg_trgm（与既有集成 conftest 一致），否则 upgrade 在
    # memory_items 的 embedding 列上炸。
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.close()
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _as_pool_url(url))
    upgrade_head(cfg)
    yield url
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.close()


def _psycopg_dsn(url: str) -> str:
    """psycopg3 只认裸 libpq 串；SQLAlchemy 的 `+psycopg` scheme 要剥掉。

    这个坑是实测踩出来的：夹具 teardown 用带 scheme 的 DSN 连接会直接抛，清理静默失效，
    于是每个用例写的 journal 行都漏给下一个用例——看起来像实现有 bug。
    """
    return url.replace("postgresql+psycopg://", "postgresql://", 1).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


def _cleanup(url: str) -> None:
    conn = psycopg.connect(_psycopg_dsn(url), autocommit=True)
    try:
        for table in _PROFILE_TABLES:
            conn.execute(
                f"DELETE FROM {table} WHERE tenant_id LIKE %s", (f"{CLEANUP_PREFIX}%",)
            )
    finally:
        conn.close()


@pytest.fixture()
def factory(c18_pg_url: str) -> Iterator[async_sessionmaker]:
    """每用例独立 engine：NullPool，不留跨 loop 的连接。"""
    engine = create_async_engine(
        _as_pool_url(c18_pg_url, psycopg=False), poolclass=NullPool
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture()
def store(factory: async_sessionmaker, c18_pg_url: str) -> Iterator[PgMemoryProfileStore]:
    _cleanup(c18_pg_url)
    instance = PgMemoryProfileStore(factory, TENANT_A)
    yield instance
    _cleanup(c18_pg_url)


class _PgHarness:
    """`ProfileStoreHarness` 的 PG 侧实现。"""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    def borrow(self, store: Any) -> Any:
        return self._sf()

    def store_for(self, tenant_id: str) -> PgMemoryProfileStore:
        return PgMemoryProfileStore(self._sf, tenant_id)

    async def count_rows(self, store: Any, table: str, category: str) -> int:
        if table not in _PROFILE_TABLES:
            raise AssertionError(f"不是画像专表：{table}")
        async with self._sf() as sess:
            return int(
                (
                    await sess.execute(
                        text(
                            f"SELECT count(*) FROM {table} "
                            "WHERE tenant_id = :t AND category = :c"
                        ),
                        {"t": store.tenant_id, "c": category},
                    )
                ).scalar_one()
            )

    async def insert_without_happened_at(self, store: Any) -> None:
        async with self._sf() as sess:
            await sess.execute(
                text(
                    "INSERT INTO tenant_memory_profile_entries "
                    "(tenant_id, category, content) VALUES (:t, 'journal', 'x')"
                ),
                {"t": store.tenant_id},
            )
            await sess.commit()

    async def commit(self, session: Any) -> None:
        await session.commit()

    async def abort(self, session: Any) -> None:
        await session.rollback()


@pytest.fixture()
def harness(factory: async_sessionmaker) -> _PgHarness:
    return _PgHarness(factory)


@pytest.mark.parametrize("case", CASES, ids=[case.__name__ for case in CASES])
async def test_pg_profile_store(
    case: Any, store: PgMemoryProfileStore, harness: _PgHarness
) -> None:
    await case(store, harness)
