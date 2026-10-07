"""C14 memory engine catalog/binding PG 集成测试 fixture（模式同 tests/persona/conftest.py）。

scratch DB ``nexus_memory_engines_test``；只清 C14 两表 + 复位种子账号
（chat_api 端点测试需要 account/canonical 会话派生身份）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.config import Config

from scripts.migrate.alembic_util import upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)
SCRATCH_DB = "nexus_memory_engines_test"


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def me_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 memory engines 集成测试")
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.close()
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url", url.replace("postgresql://", "postgresql+psycopg://")
    )
    upgrade_head(cfg)
    yield url
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.close()


@pytest.fixture
def me_factory(me_pg_url):
    engine = create_async_engine(
        me_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture(autouse=True)
def _clean_slate(me_pg_url) -> Iterator[None]:
    """每用例空基线：清 C14 两表 + 复位 chat_api 端点测试所需种子行。"""

    def _reset() -> None:
        conn = psycopg.connect(me_pg_url, autocommit=True)
        conn.execute("TRUNCATE tenant_memory_engine_bindings CASCADE")
        conn.execute("TRUNCATE tenant_memory_engine_events CASCADE")
        conn.execute("TRUNCATE test_accounts, canonical_conversations CASCADE")
        conn.execute(
            "INSERT INTO test_accounts (id, status, display_name) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 'active', 'Pilot Dev Account')"
        )
        conn.execute(
            "INSERT INTO canonical_conversations (id, tenant_id, account_id, status) "
            "VALUES ('00000000-0000-0000-0000-000000000002', 'me_dev', "
            "'00000000-0000-0000-0000-000000000001', 'active')"
        )
        conn.close()

    _reset()
    yield


@pytest.fixture
def exec_sql(me_pg_url) -> Callable[..., Any]:
    def _run(sql: str, params: tuple = ()) -> list[tuple]:
        conn = psycopg.connect(me_pg_url)
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    return _run
