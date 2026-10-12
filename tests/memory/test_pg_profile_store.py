"""c18 task 6.4：PG 侧画像存储实现。

会话级独立 scratch 库 `nexus_c18test`（同 `tests/auth_provisioning/conftest.py` 的做法，
不污染共享 `nexus` 库），空库执行 `alembic upgrade head` 拿到 b7d3f5a9c1e4 建的两张表。
本地 PG 不可用时整组 skip。

侧重点不是「能不能存进去」，而是 ADR-13 / ADR-14 换来的三条语义：整块类覆写不增行、
日志类靠 `(tenant_id, dedup_key)` 幂等且该键按租户隔离、pending 的取走与删除是一个原子
动作。最后一条取代的是文件版那套「原子重命名 + 提交/回滚」三步。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.config import Config

from agent.memory_pg import PgMemoryProfileStore
from core.memory.profile_store import MemoryProfileStoreApi
from scripts.migrate.alembic_util import upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.postgres

ADMIN_URL = "postgresql://nexus:nexus_dev@localhost:5433/nexus"
SCRATCH_DB = "nexus_c18test"
TENANT_A = "c18-pg-store-a"
TENANT_B = "c18-pg-store-b"


def _pg_alive() -> bool:
    try:
        conn = psycopg.connect(ADMIN_URL, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


def _as_pool_url(url: str, *, psycopg: bool = True) -> str:
    scheme = "postgresql+psycopg://" if psycopg else "postgresql+asyncpg://"
    return url.replace("postgresql://", scheme, 1)


@pytest.fixture(scope="session")
def c18_pg_url() -> Iterator[str]:
    if not _pg_alive():
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过画像存储 PG 集成测试")
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
    conn = psycopg.connect(url, autocommit=True)
    # 迁移链需要 vector/pg_trgm（与既有集成 conftest 一致），否则 upgrade 在
    # memory_items 的 embedding 列上炸。
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.close()
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _as_pool_url(url))
    upgrade_head(cfg)
    yield url
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.close()


def _psycopg_dsn(url: str) -> str:
    """psycopg3 只认裸 libpq 串；SQLAlchemy 的 `+psycopg` scheme 要剥掉。

    这个坑是实测踩出来的：夹具 teardown 用带 scheme 的 DSN 连接会直接抛，清理静默失效，
    于是每个用例写的 journal 行都漏给下一个用例——看起来像实现有 bug。
    """
    return url.replace("postgresql+psycopg://", "postgresql://", 1).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


def _cleanup(url: str) -> None:
    conn = psycopg.connect(_psycopg_dsn(url), autocommit=True)
    try:
        for table in (
            "tenant_memory_profile_sections",
            "tenant_memory_profile_entries",
        ):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id LIKE 'c18-pg-store-%'")
    finally:
        conn.close()


@pytest.fixture()
def factory(c18_pg_url) -> Iterator[async_sessionmaker]:
    """每用例独立 engine：NullPool，不留跨 loop 的连接。"""
    engine = create_async_engine(_as_pool_url(c18_pg_url, psycopg=False), poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    engine.sync_engine.dispose()


@pytest.fixture()
def store(factory, c18_pg_url) -> Iterator[PgMemoryProfileStore]:
    _cleanup(c18_pg_url)
    instance = PgMemoryProfileStore(factory, TENANT_A)
    yield instance
    _cleanup(c18_pg_url)


def _ts(day: int) -> datetime:
    return datetime(2026, 10, day, 8, 0, 0, tzinfo=timezone.utc)


def test_satisfies_the_protocol(store: PgMemoryProfileStore) -> None:
    assert isinstance(store, MemoryProfileStoreApi)


async def test_section_is_one_row_per_category(store: PgMemoryProfileStore) -> None:
    await store.write_section("long_term", "第一版")
    assert await store.read_section("long_term") == "第一版"
    await store.write_section("long_term", "第二版")
    assert await store.read_section("long_term") == "第二版", "整块类必须覆写而不是追加"
    # 覆写没有留下第二行。
    async with store._sf() as sess:  # noqa: SLF001
        from sqlalchemy import text as _text

        count = (
            await sess.execute(
                _text(
                    "SELECT count(*) FROM tenant_memory_profile_sections "
                    "WHERE tenant_id = :t AND category = 'long_term'"
                ),
                {"t": TENANT_A},
            )
        ).scalar_one()
    assert count == 1


async def test_missing_section_is_empty_not_a_fallback(store: PgMemoryProfileStore) -> None:
    assert await store.read_section("recent_context") == ""


async def test_category_groups_are_enforced(store: PgMemoryProfileStore) -> None:
    with pytest.raises(ValueError):
        await store.write_section("self", "不在整块组里")
    with pytest.raises(ValueError):
        await store.append_entry("long_term", "整块类不能当日志追加", happened_at=_ts(1))


async def test_entries_read_in_event_order_and_tail_to_budget(
    store: PgMemoryProfileStore,
) -> None:
    for day in (3, 1, 2):
        await store.append_entry("history", f"E{day}", happened_at=_ts(day))
    assert await store.read_entries("history") == "E1\n\nE2\n\nE3"
    assert await store.read_entries("history", max_chars=5) == "2\n\nE3"[-5:]


async def test_dedup_key_is_per_tenant(store: PgMemoryProfileStore) -> None:
    key = "consolidation:" + "m" * 400  # 远超原 app_configs.key String(128) 的长度
    assert await store.append_entry("history", "一", happened_at=_ts(1), dedup_key=key)
    assert not await store.append_entry("history", "二", happened_at=_ts(1), dedup_key=key)
    assert await store.read_entries("history") == "一"
    other = PgMemoryProfileStore(store._sf, TENANT_B)  # noqa: SLF001
    assert await other.append_entry(
        "history", "三", happened_at=_ts(1), dedup_key=key
    ), "F3：租户 B 不该被租户 A 留下的同名键静默判重"
    assert await other.read_entries("history") == "三"


async def test_null_dedup_key_always_appends(store: PgMemoryProfileStore) -> None:
    assert await store.append_entry("pending", "a", happened_at=_ts(1))
    assert await store.append_entry("pending", "b", happened_at=_ts(2))
    assert await store.read_entries("pending") == "a\n\nb"


async def test_take_pending_returns_and_empties(store: PgMemoryProfileStore) -> None:
    await store.append_entry("pending", "事实一", happened_at=_ts(1))
    await store.append_entry("pending", "事实二", happened_at=_ts(2))
    assert await store.take_pending() == "事实一\n\n事实二"
    assert await store.take_pending() == ""
    assert await store.read_entries("history") == "", "只该带走 pending"


async def test_read_assembly_uses_the_borrowed_session(
    store: PgMemoryProfileStore,
) -> None:
    await store.write_section("long_term", "长期")
    await store.write_section("recent_context", "近期")
    await store.write_section("relationship_state", "关系状态")
    await store.append_entry("pending", "待合并", happened_at=_ts(1))
    await store.append_entry("history", "历史", happened_at=_ts(1))
    await store.append_entry("journal", "不该进组装期", happened_at=_ts(1))

    async with store._sf() as sess:  # noqa: SLF001
        assembly = await store.read_assembly(sess, history_max_chars=50)

    assert assembly.tenant_id == TENANT_A
    assert assembly.long_term == "长期"
    assert assembly.recent_context == "近期"
    assert assembly.relationship_state == "关系状态", "ADR-1：与四类画像出自同一条 SELECT"
    assert assembly.pending == "待合并"
    assert assembly.history == "历史"
    assert "不该进组装期" not in (assembly.pending + assembly.history)


async def test_happened_at_is_required_by_the_schema(
    store: PgMemoryProfileStore, c18_pg_url
) -> None:
    """ADR-8 的日期占列靠建表约束，不靠实现记得填——原缺口就是这么产生的。"""
    from sqlalchemy import text as _text

    async with store._sf() as sess:  # noqa: SLF001
        with pytest.raises(Exception):
            await sess.execute(
                _text(
                    "INSERT INTO tenant_memory_profile_entries "
                    "(tenant_id, category, content) VALUES (:t, 'journal', 'x')"
                ),
                {"t": TENANT_A},
            )
        await sess.rollback()
    assert (
        await store.read_entries("journal") == ""
    ), "上面的插入必须真的没落库"
