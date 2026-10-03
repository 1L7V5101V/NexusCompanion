"""retention PG 集成测试 fixture（模式同 tests/control_plane/conftest.py）。

会话级独立 scratch DB ``nexus_retention_test``，空库上 ``alembic upgrade head``；
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

from scripts.migrate.alembic_util import upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)
SCRATCH_DB = "nexus_retention_test"

DEV_TENANT_ID = "dev"
DEV_CONVERSATION_ID = "00000000-0000-0000-0000-000000000002"
# 与 C1 migration seed 一致的固定 dev 身份（确定性断言用）。

RETENTION_TABLES = (
    "delivery_attempts",
    "outbound_delivery_intents",
    "webchat_replay_frames",
    "webchat_replay_counters",
    "work_attempts",
    "background_work_items",
    "tool_audit_events",
    "admin_audit_events",
    "tool_calls",
    "turns",
    "inbox_records",
    "message_deduplication_keys",
    "auth_sessions",
    "access_tokens",
)


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def rt_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 retention 集成测试")
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
    # 迁移链含 vector(1024)（a3d5c7e9f1b2）与 pg_trgm（b6e9d2c4a8f1）。
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
def rt_factory(rt_pg_url):
    engine = create_async_engine(
        rt_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture
def rt_reset(rt_pg_url) -> Iterator[None]:
    """每用例空基线（TRUNCATE CASCADE + 重放 dev account seed）。

    仅 PG 集成测试显式声明（config 等纯单元测试不触碰数据库）。
    """

    def _reset() -> None:
        conn = psycopg.connect(rt_pg_url, autocommit=True)
        conn.execute(
            f"TRUNCATE {', '.join(RETENTION_TABLES)}, "
            "canonical_messages, canonical_conversations, test_accounts CASCADE"
        )
        conn.execute(
            "INSERT INTO test_accounts (id, status, display_name) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 'active', 'Pilot Dev Account')"
        )
        conn.execute(
            "INSERT INTO canonical_conversations (id, tenant_id, account_id, status) "
            "VALUES ('00000000-0000-0000-0000-000000000002', 'dev', "
            "'00000000-0000-0000-0000-000000000001', 'active')"
        )
        conn.close()

    _reset()
    yield


@pytest.fixture
def make_tenant(rt_factory) -> Callable[..., Awaitable[dict[str, Any]]]:
    """创建独立 account+conversation（tenant 唯一）。"""

    from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository

    async def _make(prefix: str = "rt") -> dict[str, Any]:
        identities = CanonicalIdentityRepository(rt_factory)
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
def sweeper_rt(rt_factory: "async_sessionmaker") -> "RetentionSweeper":
    """默认配置的 RetentionSweeper（供凭据/报告类用例共享）。"""
    from agent.config_models import RetentionConfig
    from bootstrap.retention import RetentionSweeper

    return RetentionSweeper(rt_factory, RetentionConfig())


@pytest.fixture
def exec_sql(rt_pg_url) -> Callable[..., Any]:
    """直连执行 SQL（backdate 时间戳 / 造崩溃残留等不可经仓储表达的状态）。"""

    def _run(sql: str, params: tuple = ()) -> list[tuple]:
        conn = psycopg.connect(rt_pg_url)
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    return _run
