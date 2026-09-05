"""C1 canonical identity PG 集成测试 fixture。

会话级独立 scratch DB ``nexus_c1test``（不污染共享 ``nexus`` 库），在空库上执行
``alembic upgrade head``（Create→Verify 的 Create 步），供 migration / 并发
sequence / 负向测试（Verify 步，§5.9.9：测试数据与正式 test account 分离）。
本地 PG 不可用时整组 skip（``postgres`` marker 语义）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic.config import Config

from scripts.migrate.alembic_util import downgrade_to, upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)
SCRATCH_DB = "nexus_c1test"

# 与 migration seed 一致的固定 dev 身份（确定性断言用）。
DEV_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
DEV_TENANT_ID = "dev"
DEV_CONVERSATION_ID = "00000000-0000-0000-0000-000000000002"


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def c1_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 canonical identity 集成测试")
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
    # 迁移链含 vector(1024) 类型（a3d5c7e9f1b2）与 pg_trgm（b6e9d2c4a8f1）。
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
def c1_alembic_cfg(c1_pg_url) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url", c1_pg_url.replace("postgresql://", "postgresql+psycopg://")
    )
    return cfg


@pytest.fixture
def c1_factory(c1_pg_url):
    """每用例独立 async engine/session factory（NullPool：无跨 loop 连接残留）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = create_async_engine(
        c1_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture
def c1_reset(c1_pg_url) -> Callable[[], None]:
    """清空三表并重放 dev seed，保证每用例从相同空基线开始。"""

    def _reset() -> None:
        conn = psycopg.connect(c1_pg_url, autocommit=True)
        conn.execute(
            "TRUNCATE canonical_messages, canonical_conversations, test_accounts CASCADE"
        )
        conn.execute(
            "INSERT INTO test_accounts (id, status, display_name) "
            "VALUES (%s, 'active', 'Pilot Dev Account')",
            (DEV_ACCOUNT_ID,),
        )
        conn.execute(
            "INSERT INTO canonical_conversations (id, tenant_id, account_id, status) "
            "VALUES (%s, %s, %s, 'active')",
            (DEV_CONVERSATION_ID, DEV_TENANT_ID, DEV_ACCOUNT_ID),
        )
        conn.close()

    _reset()
    return _reset
