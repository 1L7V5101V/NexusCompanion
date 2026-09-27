"""C7 tool isolation PG 集成测试 fixture。

会话级独立 scratch DB ``nexus_c7test``（不污染共享 ``nexus`` 库）：空库执行
``alembic upgrade head``（含 C7 ``tool_audit_events``/C5 canonical 等全链表），
供跨租户并发交错 / 双租户目录隔离 / 关闭面 PG 验证使用。本地 PG 不可用时整组
skip（postgres marker 语义）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.config import Config

from scripts.migrate.alembic_util import upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)
SCRATCH_DB = "nexus_c7test"


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def c7_pg_url() -> Iterator[str]:
    if not _pg_alive(DATABASE_URL):
        pytest.skip(f"本地 PG 不可用（{DATABASE_URL}），跳过 C7 集成测试")
    admin = psycopg.connect(DATABASE_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = DATABASE_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
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
    admin = psycopg.connect(DATABASE_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.close()


@pytest.fixture
def c7_factory(c7_pg_url) -> Iterator[async_sessionmaker]:
    """每用例独立 async engine/session factory（NullPool：无跨 loop 连接残留）。"""
    engine = create_async_engine(
        c7_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture
def c7_reset(c7_pg_url) -> None:
    """清空 canonical/auth/审计表，保证每用例从相同空基线开始。"""
    conn = psycopg.connect(c7_pg_url, autocommit=True)
    conn.execute(
        "TRUNCATE access_tokens, auth_sessions, admin_credentials, "
        "admin_audit_events, tenant_provisioning_jobs, tool_audit_events, "
        "canonical_messages, canonical_conversations, test_accounts CASCADE"
    )
    conn.close()


@pytest.fixture
def c7_runtime(c7_pg_url, c7_reset, tmp_path):
    """完整 AuthRuntime（canonical repo 自建；pepper 落在独立 workspace）。"""
    from agent.config_models import Config

    from bootstrap.auth.runtime import create_auth_runtime

    cfg = Config(provider="", model="", api_key="", system_prompt="")
    cfg.storage.postgres_url = c7_pg_url.replace(
        "postgresql://", "postgresql+asyncpg://"
    )
    runtime = create_auth_runtime(config=cfg, workspace=tmp_path / "ws")
    yield runtime
    import asyncio

    try:
        asyncio.run(runtime.aclose())
    except RuntimeError:
        pass


@pytest.fixture
def c7_tenants(c7_runtime):
    """回归共同基线：两个 active 账号（各一个 agent → 独立 tenant）。"""

    async def _make() -> dict:
        tenants: dict[str, dict] = {}
        for label, display in (("a", "Tenant A"), ("b", "Tenant B")):
            created = await c7_runtime.provisioning.create_account(
                display_name=display
            )
            await c7_runtime.provisioning.run_pending(max_jobs=4)
            account = await c7_runtime.provisioning.get_account(
                created["account"]["id"]
            )
            assert account["status"] == "active"
            convs = await c7_runtime.canonical_repo.list_conversations_by_account(
                created["account"]["id"]
            )
            assert len(convs) == 1
            tenants[label] = {
                "account_id": str(created["account"]["id"]),
                "tenant_id": str(convs[0]["tenant_id"]),
                "conversation_id": str(convs[0]["id"]),
            }
        return tenants

    return _make