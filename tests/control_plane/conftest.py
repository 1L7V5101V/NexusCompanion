"""C2 durable control plane PG 集成测试 fixture。

会话级独立 scratch DB ``nexus_c2test``（不污染共享 ``nexus`` 库），在空库上执行
``alembic upgrade head``（Create 步），供 migration / 三事务 / 状态机 / 重启重放
测试（Verify 步，§5.9.9：测试数据与正式 test account 分离）。
本地 PG 不可用时整组 skip（``postgres`` marker 语义）。
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.config import Config

from scripts.migrate.alembic_util import downgrade_to, upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)
SCRATCH_DB = "nexus_c2test"

# 与 C1 migration seed 一致的固定 dev 身份（确定性断言用）。
DEV_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
DEV_TENANT_ID = "dev"
DEV_CONVERSATION_ID = "00000000-0000-0000-0000-000000000002"

C2_TABLES = (
    "delivery_attempts",
    "outbound_delivery_intents",
    "background_work_items",
    "tool_calls",
    "turns",
    "inbox_records",
    "message_deduplication_keys",
)


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def c2_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 control plane 集成测试")
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
def c2_alembic_cfg(c2_pg_url) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url", c2_pg_url.replace("postgresql://", "postgresql+psycopg://")
    )
    return cfg


@pytest.fixture
def c2_factory(c2_pg_url):
    """每用例独立 async engine/session factory（NullPool：无跨 loop 连接残留）。"""
    engine = create_async_engine(
        c2_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture
def c2_reset(c2_pg_url) -> Callable[[], None]:
    """清空 control plane + canonical 表并重放 dev seed，保证空基线。"""

    def _reset() -> None:
        conn = psycopg.connect(c2_pg_url, autocommit=True)
        table_list = ", ".join(C2_TABLES)
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
def _clean_slate(c2_reset: Callable[[], None]) -> None:
    """每用例空基线：claim/投递断言依赖全局扫描（delivery worker 无租户过滤），
    必须逐用例隔离，否则跨用例残留 intent 会被互相认领。"""
    c2_reset()


@pytest.fixture
def make_tenant(c2_factory) -> Callable[..., Awaitable[dict[str, Any]]]:
    """创建独立 account+conversation（tenant 唯一，不重置全库）。

    需要空库基线的测试（断言全局计数）应显式先调用 ``c2_reset()``；
    多租户并存测试不可在租户创建之间 reset。
    """

    from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository

    async def _make(prefix: str = "c2") -> dict[str, Any]:
        identities = CanonicalIdentityRepository(c2_factory)
        provisioned = await identities.provision_account_with_agent(
            f"{prefix}_{uuid.uuid4().hex[:10]}", status="active"
        )
        return {
            "tenant_id": provisioned["conversation"]["tenant_id"],
            "account_id": provisioned["account"]["id"],
            "conversation_id": provisioned["conversation"]["id"],
        }

    return _make


@pytest.fixture
def exec_sql(c2_pg_url) -> Callable[..., Any]:
    """直连执行 SQL（模拟时间流逝 / 崩溃残留等不可经仓储表达的状态）。"""

    def _run(sql: str, params: tuple = ()) -> list[tuple]:
        conn = psycopg.connect(c2_pg_url)
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    return _run


@pytest.fixture
def downgrade_engine(c2_alembic_cfg) -> Callable[[str], None]:
    return lambda rev: downgrade_to(c2_alembic_cfg, rev)
