"""C11 显式用户 schedule PG 集成测试 fixture。

沿用 `tests/control_plane/conftest.py` 的会话级独立 scratch DB 模式：建库
``nexus_c11test`` → 空库 `alembic upgrade head`（含 e8b4c2a6d9f1 两表）→ 每用例
TRUNCATE + 重放 dev seed。本地 PG 不可用时整组 skip（``postgres`` marker 语义）。
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
SCRATCH_DB = "nexus_c11test"

DEV_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
DEV_TENANT_ID = "dev"
DEV_CONVERSATION_ID = "00000000-0000-0000-0000-000000000002"

# execution 行对 intent/message 是 FK RESTRICT，清理必须连 C2 表一起截断。
TRUNCATE_TABLES = (
    "schedule_executions",
    "scheduled_jobs",
    "webchat_replay_frames",
    "webchat_replay_counters",
    "delivery_attempts",
    "outbound_delivery_intents",
    "background_work_items",
    "tool_calls",
    "turns",
    "inbox_records",
    "message_deduplication_keys",
    "canonical_messages",
    "canonical_conversations",
    "test_accounts",
)

MISFIRE_GRACE_SECONDS = 300


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def c11_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 C11 schedule 集成测试")
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
def c11_factory(c11_pg_url):
    """每用例独立 async engine/session factory（NullPool：无跨 loop 连接残留）。"""
    engine = create_async_engine(
        c11_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture
def c11_reset(c11_pg_url) -> Callable[[], None]:
    """清空 schedule/control plane/canonical 表并重放 dev seed，保证空基线。"""

    def _reset() -> None:
        conn = psycopg.connect(c11_pg_url, autocommit=True)
        conn.execute(f"TRUNCATE {', '.join(TRUNCATE_TABLES)} CASCADE")
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
def _clean_slate(request: pytest.FixtureRequest) -> None:
    """PG 用例每例空基线；纯计算 contract 用例不拉 DB（无 PG 环境也能跑）。

    tick 认领与 intent 计数是全局扫描，跨用例残留会互相认领，必须逐用例重置。
    """
    if request.node.get_closest_marker("postgres") is None:
        return
    request.getfixturevalue("c11_reset")()


@pytest.fixture
def make_tenant(c11_factory) -> Callable[..., Awaitable[dict[str, Any]]]:
    """创建独立 active account + canonical conversation（tenant 唯一）。"""

    from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository

    async def _make(prefix: str = "c11") -> dict[str, Any]:
        identities = CanonicalIdentityRepository(c11_factory)
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
def set_account_status(c11_pg_url) -> Callable[[str, str], None]:
    """直接改 test_accounts.status（suspend/revoke 联动用例用）。"""

    def _set(account_id: str, status: str) -> None:
        conn = psycopg.connect(c11_pg_url, autocommit=True)
        conn.execute("UPDATE test_accounts SET status = %s WHERE id = %s", (status, account_id))
        conn.close()

    return _set


@pytest.fixture
def exec_sql(c11_pg_url) -> Callable[..., Any]:
    """直连执行 SQL（模拟崩溃残留、回填时间等不可经仓储表达的状态）。"""

    def _run(sql: str, params: tuple = ()) -> list[tuple]:
        conn = psycopg.connect(c11_pg_url)
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    return _run


@dataclass
class Clock:
    """可推进的假钟：durable 调度的到期判据全部经它注入，不用 sleep 等真实时间。"""

    now: datetime = field(
        default_factory=lambda: datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    )

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now

    def set(self, when: datetime) -> datetime:
        self.now = when
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo(c11_factory):
    from bootstrap.db.repository.schedule_repo import ScheduleRepository

    return ScheduleRepository(
        c11_factory, misfire_grace_seconds=MISFIRE_GRACE_SECONDS
    )


@pytest.fixture
def make_job(repo, clock) -> Callable[..., Awaitable[dict[str, Any]]]:
    """按 durable 创建面的入参落一行 job（binding 由服务端组装的等价物）。"""

    async def _make(
        tenant: dict[str, Any],
        **overrides: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "tenant_id": tenant["tenant_id"],
            "trigger": "at",
            "tier": "instant",
            "when": "14:30",
            "fire_at": clock.now + timedelta(minutes=5),
            "timezone": "UTC",
            "delivery_channel": "chat",
            "delivery_target": tenant["tenant_id"],
            "message": "该喝水了",
        }
        params.update(overrides)
        return await repo.create_job(**params)

    return _make


@pytest.fixture
def build_service(c11_factory, clock) -> Callable[..., Any]:
    """构造被测调度服务：假钟 + 可选 agent_loop/revocation_gate/宽限。"""

    from bootstrap.schedule_durable import DurableSchedulerService

    def _build(**kwargs: Any) -> DurableSchedulerService:
        kwargs.setdefault("misfire_grace_seconds", MISFIRE_GRACE_SECONDS)
        return DurableSchedulerService(
            session_factory=c11_factory, _now_fn=clock, **kwargs
        )

    return _build


@pytest.fixture
def run_ticks(build_service) -> Callable[..., Any]:
    """跑 N 轮 tick，并 await 本轮派发的执行任务直到收束完成。

    durable 的收束发生在 tick 派发的子任务里（soft 的 AI 调用不能阻塞 tick），
    所以断言前必须真正 await 这些任务，而不是让出若干事件循环切片。
    """

    import asyncio

    async def _run(service, *, times: int = 1) -> None:
        for _ in range(times):
            await service._tick_once()
            pending = [t for t in service._tasks if not t.done()]
            while pending:
                await asyncio.gather(*pending, return_exceptions=True)
                # done_callback 经 call_soon 摘除任务，需要一轮调度才生效。
                await asyncio.sleep(0)
                pending = [t for t in service._tasks if not t.done()]

    return _run
