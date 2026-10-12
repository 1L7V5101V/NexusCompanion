"""c18 task 8.3：跨租户隔离断言，两种后端各跑一遍。

判据来自 spec 的四条 Scenario：「并发双租户写不互串」「单机后端也能验隔离」
「召回不返回画像内容」「关系状态不出现在召回面」。这里把它们压成一轮可执行的检查：
租户 A 写满六类画像内容（三类整块 + 三类日志，含 `relationship_state`），然后切到
租户 B 组装一轮，断言 A 的 canary 既不在 B 的系统提示里、也不在召回面那张
`memory_items` 里。

为什么 SQLite 也能跑：两张画像专表都带 `tenant_id` 列（ADR-10 补的），所以单机后端不再
像 `memory_items` 那样只能验「只有一个租户」——那张表连 `tenant_id` 列都没有，这恰好是
「画像不能与召回共表」那条断言的物证。

PG 一侧复用 `test_pg_profile_store.py` 的会话级 scratch 库（本地 PG 不可用时整组 skip，
不拿 skip 冒充通过）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

# 复用同一份 scratch 库夹具：PG 不可用时它自己 skip，本文件的 PG 用例随之 skip
from tests.memory.test_pg_profile_store import c18_pg_url, _as_pool_url  # noqa: F401
from agent.context import ContextBuilder
from agent.core.prompt_block import (
    SelfModelPromptBlock,
    TurnContext,
)
from agent.core.types import ContextRequest, MemoryProfileSnapshot, PersonaSnapshot
from core.memory.profile_sqlite import SqliteMemoryProfileStore
from tests.memory.test_sqlite_profile_store import create_sqlite_memory_db

TENANT_A = "c18-iso-a"
TENANT_B = "c18-iso-b"
CANARY = "硅的光子挠曲电效应与声子隔离"
_TS = datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)


class _Side:
    """一个后端上「两个租户、一次借用」的形状。

    `assembly(tenant)` 就是组装期那一次借用：PG 侧是一个 `AsyncSession`，SQLite 侧是一个
    同步事务体整个交给 `asyncio.to_thread` 跑完——把 `transaction()` 持在两次 `await` 之间
    会在另一个工作线程上等同一把 `RLock`（ADR-14 里写死的那条限制，这里必须照同一个形状写）。
    """

    def __init__(
        self,
        store_for: Any,
        assembly_for: Any,
        count_in_memory_items: Any,
    ) -> None:
        self._store_for = store_for
        self._assembly_for = assembly_for
        self._count = count_in_memory_items

    def store(self, tenant_id: str) -> Any:
        return self._store_for(tenant_id)

    async def assembly(self, tenant_id: str) -> Any:
        return await self._assembly_for(tenant_id)

    def memory_items_containing(self, fragment: str) -> int:
        return self._count(fragment)


async def _seed_tenant_a(side: _Side) -> None:
    store = side.store(TENANT_A)
    await store.write_section("long_term", f"## 用户事实\n- {CANARY}")
    await store.write_section(
        "recent_context", f"## Compression\n- 待延续话题：{CANARY}"
    )
    await store.write_section("relationship_state", f"我们关系的定义：{CANARY}")
    await store.append_entry("pending", f"- [identity] {CANARY}", happened_at=_TS)
    await store.append_entry("history", f"[2026-03-08 12:00] {CANARY}", happened_at=_TS)
    await store.append_entry("journal", f"[2026-03-08 12:00] {CANARY}", happened_at=_TS)


def _persona(tenant_id: str, relationship_state: str) -> PersonaSnapshot:
    return PersonaSnapshot(
        tenant_id=tenant_id,
        source="custom",
        identity="Nexus",
        personality_rules="",
        relationship_state=relationship_state,
    )


def _turn_context(workspace: Path, snapshot: MemoryProfileSnapshot) -> TurnContext:
    return TurnContext(
        workspace=workspace,
        skills=cast(Any, None),
        skill_names=[],
        channel="chat",
        chat_id="c1",
        retrieved_memory_block="",
        tenant_id=snapshot.tenant_id,
        persona_snapshot=_persona(snapshot.tenant_id, ""),
        profile_snapshot=snapshot,
    )


async def _assert_isolated(side: _Side, workspace: Path) -> None:
    await _seed_tenant_a(side)

    assembly = await side.assembly(TENANT_B)
    snapshot_b = MemoryProfileSnapshot(
        tenant_id=TENANT_B,
        long_term=assembly.long_term,
        pending=assembly.pending,
        recent_context=assembly.recent_context,
        history=assembly.history,
    )
    assert snapshot_b.long_term == "", "B 组装到了 A 的长期画像"
    assert snapshot_b.pending == "" and snapshot_b.history == ""

    builder = ContextBuilder(workspace)
    result = builder.render_sections(
        ContextRequest(
            history=[],
            current_message="今天聊点什么",
            tenant_id=TENANT_B,
            profile_snapshot=snapshot_b,
            message_timestamp=_TS,
        )
    )
    joined = "\n".join(
        section.content
        for section in [*result.system_sections, *result.frame_sections]
    )
    assert CANARY not in joined, "A 的画像内容进了 B 的系统提示"
    # 空画像在 B 这一轮就是不注入，而不是回退去读别人/共享位置
    assert "Long-term Memory" not in joined

    ctx = _turn_context(workspace, snapshot_b)
    assert SelfModelPromptBlock().render(ctx) is None, "关系状态块在 B 这一轮不该出声"

    assert side.memory_items_containing(CANARY) == 0, (
        "画像内容漏进了召回面的 memory_items（spec「召回不返回画像内容」）"
    )


@pytest.fixture()
def sqlite_side(tmp_path: Path) -> Iterator[_Side]:
    db_path = tmp_path / "memory2.db"
    create_sqlite_memory_db(db_path)
    stores: dict[str, SqliteMemoryProfileStore] = {}

    def store_for(tenant_id: str) -> SqliteMemoryProfileStore:
        store = stores.get(tenant_id)
        if store is None:
            store = SqliteMemoryProfileStore(db_path, tenant_id)
            stores[tenant_id] = store
        return store

    def assembly_for(tenant_id: str) -> Any:
        async def run() -> Any:
            def sync_body() -> Any:
                store = store_for(tenant_id)
                with store.transaction() as db:
                    return store.read_assembly_sync(db, 0)

            return await asyncio.to_thread(sync_body)

        return run()

    def count(fragment: str) -> int:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE summary LIKE ?",
                (f"%{fragment}%",),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()
        return int(row[0])

    try:
        yield _Side(store_for, assembly_for, count)
    finally:
        for store in stores.values():
            # `-W error` 下未关的 sqlite 连接会由 GC 在后面的用例里报出来并算到别人头上
            store.close()


@pytest.fixture()
def pg_side(c18_pg_url: str) -> Iterator[_Side]:  # noqa: F811：夹具由参数名注入
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    import psycopg

    # asyncpg 而非 psycopg：psycopg3 的 async 在 Windows 的 ProactorEventLoop 上直接拒绝
    # （与 `test_pg_profile_store.py` 同一个选择，理由不在这里重述）
    engine = create_async_engine(
        _as_pool_url(c18_pg_url, psycopg=False), poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def store_for(tenant_id: str) -> Any:
        from agent.memory_pg import PgMemoryProfileStore

        return PgMemoryProfileStore(factory, tenant_id)

    def assembly_for(tenant_id: str) -> Any:
        async def run() -> Any:
            async with factory() as session:
                return await store_for(tenant_id).read_assembly(session)

        return run()

    def count(fragment: str) -> int:
        conn = psycopg.connect(c18_pg_url, autocommit=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE summary LIKE %s",
                (f"%{fragment}%",),
            ).fetchone()
        finally:
            conn.close()
        return int(row[0])

    side = _Side(store_for, assembly_for, count)
    try:
        yield side
    finally:
        # NullPool 不留连接跨 loop：engine 是这个 fixture 建的，就该由它关
        engine.sync_engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_two_tenants_are_isolated_end_to_end(
    tmp_path: Path, sqlite_side: _Side
) -> None:
    await _assert_isolated(sqlite_side, tmp_path)


@pytest.mark.asyncio
async def test_postgres_two_tenants_are_isolated_end_to_end(
    tmp_path: Path, pg_side: _Side
) -> None:
    await _assert_isolated(pg_side, tmp_path)
