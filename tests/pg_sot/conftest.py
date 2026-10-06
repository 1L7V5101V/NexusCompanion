"""pg-durable-sot-cutover e2e fixture（task 7.1）。

镜像 tests/auth_provisioning/conftest.py 的 C5 模式：会话级独立 scratch DB
``nexus_sottest_e2e``，空库 `alembic upgrade head`（Create），真实 C5
provisioning + AuthRuntime（Verify）；本地 PG 不可用整组 skip。
"""

from __future__ import annotations

import logging
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
SCRATCH_DB = "nexus_sottest_e2e"


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def sot_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 pg_sot e2e")
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
def sot_factory(sot_pg_url):
    engine = create_async_engine(
        sot_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture
def sot_runtime(sot_pg_url, tmp_path):
    """真实 AuthRuntime（C5 provisioning + canonical repo + auth 服务）。"""
    from agent.config_models import AuthConfig, Config
    from bootstrap.auth.runtime import create_auth_runtime

    cfg = Config(provider="", model="", api_key="", system_prompt="")
    cfg.storage.postgres_url = sot_pg_url.replace(
        "postgresql://", "postgresql+asyncpg://"
    )
    cfg.auth = AuthConfig(
        cookie_secure=False,
        origin_allowlist=["http://localhost:5173"],
        admin_allow_ips=["127.0.0.1", "::1"],
    )
    runtime = create_auth_runtime(config=cfg, workspace=tmp_path / "ws")
    yield runtime
    import asyncio

    try:
        asyncio.run(runtime.aclose())
    except RuntimeError:
        pass


@pytest.fixture
def sot_reset(sot_pg_url) -> Callable[[], None]:
    def _reset() -> None:
        conn = psycopg.connect(sot_pg_url, autocommit=True)
        conn.execute(
            "TRUNCATE access_tokens, auth_sessions, admin_credentials, "
            "admin_audit_events, tenant_provisioning_jobs, turns, inbox_records, "
            "message_deduplication_keys, outbound_delivery_intents, "
            "delivery_attempts, tool_calls, tool_audit_events, "
            "webchat_replay_frames, webchat_replay_counters, "
            "telegram_identity_bindings, telegram_binding_codes, "
            "canonical_messages, canonical_conversations, test_accounts CASCADE"
        )
        conn.close()

    _reset()
    return _reset
