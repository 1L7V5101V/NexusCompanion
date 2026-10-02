"""C6 attachment metadata PG 集成测试 fixture。

独立 scratch DB ``nexus_c6test``（不污染共享 ``nexus`` 库），空库上 ``alembic
upgrade head``（含 e6f1a3b5c7d9），供 attachment 仓储 / 生命周期测试。本地 PG
不可用时整组 skip（与 tests/control_plane 同模式）。

fixture：
- ``att_factory``：每用例独立 async engine/session factory（NullPool）；
- ``att_reset``：TRUNCATE attachment 表并重放 dev seed（固定 account/tenant/
  conversation），保证空基线；
- ``att_tenant``：确定的 (account_id, tenant_id, conversation_id) 测试身份。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

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
SCRATCH_DB = "nexus_c6test"

DEV_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
DEV_TENANT_ID = "dev"
DEV_CONVERSATION_ID = "00000000-0000-0000-0000-000000000002"

ATT_TABLES = ("attachments", "message_attachments")


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def att_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 C6 attachment 集成测试")
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.close()
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
def att_factory(att_pg_url: str) -> Iterator[async_sessionmaker]:
    engine = create_async_engine(
        att_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture
def att_reset(att_pg_url: str) -> Callable[[], None]:
    """清空 attachment 表并重放 dev seed。"""

    def _reset() -> None:
        conn = psycopg.connect(att_pg_url, autocommit=True)
        table_list = ", ".join(ATT_TABLES)
        conn.execute(
            f"TRUNCATE {table_list}, canonical_messages, canonical_conversations, "
            "test_accounts CASCADE"
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


@pytest.fixture(autouse=True)
def _att_clean_slate(att_reset: Callable[[], None]) -> None:
    att_reset()


@pytest.fixture
def att_tenant() -> dict[str, str]:
    return {
        "account_id": DEV_ACCOUNT_ID,
        "tenant_id": DEV_TENANT_ID,
        "conversation_id": DEV_CONVERSATION_ID,
    }