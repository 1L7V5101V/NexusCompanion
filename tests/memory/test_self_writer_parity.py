"""c18 task 6.6：关系状态写缝在两种后端上语义一致（design ADR-9 / ADR-10）。

这三条断言各自对着一个旧缺陷：

1. 整行覆写而不是追加——文件时代 `write_self` 与召回行之间曾互相覆盖。
2. 每次写入留一条同形态审计行（`actor='optimizer'` / `action='relationship_update'` /
   detail 只有字符数）——PG 侧原来用 `str(hash(正文))` 当幂等键，那个键既不稳定也跨租户串。
3. 召回表里不再有 `memory_type='self'` 行——ADR-13 分表要消掉的例外，用户最私密的一格
   不该被一次普通召回整段返回。

本文件整组打 `postgres` 标记：要验的命题是「两侧一致」，本地 PG 不可用时它只能整组跳过，
不能退化成「只跑了一侧也算过」。SQLite 侧独立可跑的部分（事务回滚不留半个写入）在
`tests/memory/test_sqlite_profile_store.py` 里。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from agent.memory_pg import PgMemoryProfileStore
from bootstrap.persona import PgRelationshipIO, SqliteRelationshipIO
from core.memory.profile_sqlite import SqliteMemoryProfileStore
from memory2.store import SCHEMA
from tests.memory.test_pg_profile_store import _as_pool_url, c18_pg_url

pytestmark = pytest.mark.postgres


def _detail(value: Any) -> dict[str, Any]:
    """PG 的 jsonb 经驱动可能回 dict，也可能回字符串；两侧归一成 dict 再比。"""
    if isinstance(value, str):
        return dict(json.loads(value))
    return dict(value or {})


class _PgWriter:
    backend = "pg"

    def __init__(self, session_factory: async_sessionmaker, tenant_id: str) -> None:
        self._sf = session_factory
        self.tenant_id = tenant_id
        self.io = PgRelationshipIO(session_factory)
        self._store = PgMemoryProfileStore(session_factory, tenant_id)

    async def current(self) -> str:
        return await self._store.read_section("relationship_state")

    async def audit_rows(self) -> list[dict[str, Any]]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    text(
                        "SELECT actor, action, detail FROM persona_audit_events "
                        "WHERE tenant_id = :t ORDER BY created_at"
                    ),
                    {"t": self.tenant_id},
                )
            ).all()
        return [
            {"actor": str(r[0]), "action": str(r[1]), "detail": _detail(r[2])}
            for r in rows
        ]

    async def self_rows_in_recall(self) -> int:
        async with self._sf() as sess:
            count = (
                await sess.execute(
                    text(
                        "SELECT count(*) FROM memory_items "
                        "WHERE tenant_id = :t AND memory_type = 'self'"
                    ),
                    {"t": self.tenant_id},
                )
            ).scalar_one()
        return int(count)


class _SqliteWriter:
    backend = "sqlite"

    def __init__(self, db_path: Any) -> None:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn
        # 单机只有 DEFAULT_TENANT，写缝对别的租户直接抛（见 SqliteRelationshipIO）。
        self._store = SqliteMemoryProfileStore(db_path)
        self.tenant_id = self._store.tenant_id
        self.io = SqliteRelationshipIO(self._store)

    def close(self) -> None:
        # 两条连接都要关：本夹具自己开一条读审计行，store 又开一条。少关一条会在
        # 之后的某个用例里以 ResourceWarning（unclosed database）形式炸出来——
        # `-W error` 下 GC 时点不确定，报错会算到别人头上。
        self._store.close()
        self._conn.close()

    async def current(self) -> str:
        return await self._store.read_section("relationship_state")

    async def audit_rows(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT actor, action, detail FROM persona_audit_events "
            "WHERE tenant_id = ? ORDER BY created_at",
            (self.tenant_id,),
        ).fetchall()
        return [
            {"actor": str(r[0]), "action": str(r[1]), "detail": _detail(r[2])}
            for r in rows
        ]

    async def self_rows_in_recall(self) -> int:
        # memory_items 在本后端没有 tenant_id 列（单机一个库一个用户），所以不按租户过滤。
        row = self._conn.execute(
            "SELECT count(*) FROM memory_items WHERE memory_type = 'self'"
        ).fetchone()
        return int(row[0])


@pytest.fixture()
def writer(request, tmp_path, c18_pg_url: str) -> Iterator[Any]:
    if request.param == "pg":
        engine = create_async_engine(
            _as_pool_url(c18_pg_url, psycopg=False), poolclass=NullPool
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield _PgWriter(factory, f"c18sw{uuid4().hex[:12]}")
        engine.sync_engine.dispose()
    else:
        instance = _SqliteWriter(tmp_path / "memory2.db")
        yield instance
        instance.close()


@pytest.mark.parametrize("writer", ["pg", "sqlite"], indirect=True)
async def test_write_overwrites_the_whole_row(writer: Any) -> None:
    await writer.io.write(writer.tenant_id, "第一版关系状态")
    assert await writer.current() == "第一版关系状态"
    await writer.io.write(writer.tenant_id, "第二版关系状态")
    assert await writer.current() == "第二版关系状态", "当前值必须是后一次的完整结果"


@pytest.mark.parametrize("writer", ["pg", "sqlite"], indirect=True)
async def test_each_write_leaves_one_audit_row(writer: Any) -> None:
    await writer.io.write(writer.tenant_id, "一")
    await writer.io.write(writer.tenant_id, "二二")
    rows = await writer.audit_rows()
    assert [r["action"] for r in rows] == ["relationship_update"] * 2
    assert {r["actor"] for r in rows} == {"optimizer"}
    assert [r["detail"]["chars"] for r in rows] == [1, 2], (
        "detail 记字符数，不复制正文（ADR-5）"
    )


@pytest.mark.parametrize("writer", ["pg", "sqlite"], indirect=True)
async def test_recall_table_gets_no_self_row(writer: Any) -> None:
    """写关系状态不再往召回表落 self 行——ADR-13 分表要消掉的那个例外。"""
    await writer.io.write(writer.tenant_id, "私密事实")
    assert await writer.self_rows_in_recall() == 0
    assert await writer.current() == "私密事实"


@pytest.mark.parametrize("writer", ["pg", "sqlite"], indirect=True)
async def test_read_of_never_written_tenant_is_empty(writer: Any) -> None:
    assert await writer.io.read(writer.tenant_id) == ""
