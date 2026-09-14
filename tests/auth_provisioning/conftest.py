"""C5 auth/provisioning PG 集成测试 fixture（同 C1 conftest 模式）。

会话级独立 scratch DB ``nexus_c5test``（不污染共享 ``nexus`` 库）：空库执行
``alembic upgrade head``（含 C5 migration b7e2f9a4c1d8 创建的
``access_tokens``/``auth_sessions``/``admin_credentials``/``admin_audit_events``/
``tenant_provisioning_jobs`` 五表），供 migration 约束 / 并发兑换 / provisioning
状态机 / HTTP 契约测试使用。本地 PG 不可用时整组 skip（postgres marker 语义）。

注：本组测试当前按「暂不跑 PG」决策**只写不跑**；PG 可用后取消 skip 即得完整
集成证据（证据落 ``openspec/evidence/c5-auth-provisioning-admin/``）。
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
SCRATCH_DB = "nexus_c5test"

# 固定测试身份（确定性断言用）。
ACCOUNT_A = "10000000-0000-0000-0000-0000000000a1"
ACCOUNT_B = "10000000-0000-0000-0000-0000000000b2"


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def c5_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 auth/provisioning 集成测试")
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 auth/provisioning 集成测试")
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
    conn = psycopg.connect(url, autocommit=True)
    # 上游 revision 需要 vector/pg_trgm extension（full chain 走 upgrade head）。
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
def c5_alembic_cfg(c5_pg_url) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url", c5_pg_url.replace("postgresql://", "postgresql+psycopg://")
    )
    return cfg


@pytest.fixture
def c5_factory(c5_pg_url):
    """每用例独立 async engine/session factory（NullPool：无跨 loop 连接残留）。"""
    engine = create_async_engine(
        c5_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture
def c5_reset(c5_pg_url) -> Callable[[], None]:
    """清空五表 + 三 canonical 表，保证每用例从相同空基线开始。"""

    def _reset() -> None:
        conn = psycopg.connect(c5_pg_url, autocommit=True)
        conn.execute(
            "TRUNCATE access_tokens, auth_sessions, admin_credentials, "
            "admin_audit_events, tenant_provisioning_jobs, "
            "canonical_messages, canonical_conversations, test_accounts CASCADE"
        )
        conn.close()

    _reset()
    return _reset


@pytest.fixture
def c5_workspace(tmp_path) -> Path:
    """每个用例独立 workspace（pepper 落在 <workspace>/secrets/auth_pepper）。"""
    return tmp_path / "ws"


@pytest.fixture
def c5_runtime(c5_pg_url, c5_workspace):
    """完整 AuthRuntime（canonical repo 自建；pepper 落在独立 workspace）。"""
    from agent.config_models import Config

    from bootstrap.auth.runtime import create_auth_runtime

    cfg = Config(provider="", model="", api_key="")
    cfg.storage.postgres_url = c5_pg_url
    runtime = create_auth_runtime(config=cfg, workspace=c5_workspace)
    yield runtime
    # NullPool engine 无跨 loop 残留；测试尾无需保留运行 loop。
    import asyncio

    try:
        asyncio.run(runtime.aclose())
    except RuntimeError:
        pass


@pytest.fixture
def c5_seed_account(c5_runtime, c5_reset):
    """回归共同基线：一个 active 账号（无 token/session）。"""
    c5_reset()
    _state: dict = {}

    async def _seed(display_name: str = "Seed") -> dict:
        if "account" not in _state:
            created = await c5_runtime.provisioning.create_account(display_name=display_name)
            await c5_runtime.provisioning.run_pending(max_jobs=4)
            _state["account"] = await c5_runtime.provisioning.get_account(
                created["account"]["id"]
            )
        return _state["account"]

    return _seed