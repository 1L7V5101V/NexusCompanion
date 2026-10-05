"""C9 persona/relationship PG 集成测试 fixture（模式同 tests/retention/conftest.py）。

scratch DB ``nexus_persona_test``；`make_tenant` 额外创建 memory_items 租户分区
（onboarding 种子与快照读取的前置，真实时序由 provisioning 就绪服务完成）。
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
SCRATCH_DB = "nexus_persona_test"


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def persona_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 persona 集成测试")
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
def persona_factory(persona_pg_url):
    engine = create_async_engine(
        persona_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture(autouse=True)
def _clean_slate(persona_pg_url) -> Iterator[None]:
    """每用例空基线：清 persona/审计/种子行 + 删除测试分区。"""

    def _reset() -> None:
        conn = psycopg.connect(persona_pg_url, autocommit=True)
        cur = conn.execute(
            "SELECT inhrelid::regclass::text FROM pg_inherits "
            "WHERE inhparent='memory_items'::regclass AND inhrelid::regclass::text LIKE 'memory_items_pt_%'"
        )
        parts = [r[0] for r in cur.fetchall()]
        conn.execute(
            "TRUNCATE tenant_persona_profiles, persona_audit_events, "
            "persona_templates, test_accounts, canonical_conversations CASCADE"
        )
        conn.execute(
            "INSERT INTO test_accounts (id, status, display_name) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 'active', 'Pilot Dev Account')"
        )
        conn.execute(
            "INSERT INTO canonical_conversations (id, tenant_id, account_id, status) "
            "VALUES ('00000000-0000-0000-0000-000000000002', 'pt_dev', "
            "'00000000-0000-0000-0000-000000000001', 'active')"
        )
        for name in parts:
            conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.execute(
            "CREATE TABLE memory_items_pt_dev PARTITION OF memory_items "
            "FOR VALUES IN ('pt_dev')"
        )
        conn.close()

    _reset()
    yield


@pytest.fixture
def make_tenant(persona_factory) -> Callable[..., Awaitable[dict[str, Any]]]:
    """创建独立 account+conversation + memory 分区（provisioning 前置替身）。"""

    from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository
    from infra.storage.partitioning import partition_name_for_tenant

    async def _make(prefix: str = "pt") -> dict[str, Any]:
        identities = CanonicalIdentityRepository(persona_factory)
        provisioned = await identities.provision_account_with_agent(
            f"{prefix}_{uuid.uuid4().hex[:10]}", status="active"
        )
        tenant_id = str(provisioned["conversation"]["tenant_id"])
        pname = partition_name_for_tenant(tenant_id)
        async with persona_factory() as sess, sess.begin():
            await sess.execute(
                __import__("sqlalchemy").text(
                    f"CREATE TABLE {pname} PARTITION OF memory_items "
                    f"FOR VALUES IN ('{tenant_id}')"
                )
            )
        return {
            "tenant_id": tenant_id,
            "account_id": provisioned["account"]["id"],
            "conversation_id": provisioned["conversation"]["id"],
        }

    return _make


@pytest.fixture
def persona_repo(persona_factory):
    from bootstrap.db.repository.persona_repo import PersonaRepository

    return PersonaRepository(persona_factory)


@pytest.fixture
def exec_sql(persona_pg_url) -> Callable[..., Any]:
    def _run(sql: str, params: tuple = ()) -> list[tuple]:
        conn = psycopg.connect(persona_pg_url)
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    return _run
