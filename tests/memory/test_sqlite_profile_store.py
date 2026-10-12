"""c18 task 6.5：SQLite 侧画像存储实现。

用例正文与 PG 侧共用 `tests/memory/profile_store_cases.py` 的那一份清单——两侧断言不各自
漂移，这是 ADR-10 补四张表之后唯一的兑现方式（单机没有第二个租户，但隔离断言在这里跑得
起来）。本文件只提供 SQLite 特有的三件事，外加两条本后端才有的承重断言。

建表用的是 `memory2/store.py` 里那份 `SCHEMA` 常量本身，不是测试再抄一遍：这样本用例验的
就是单机进程真正会拿到的 DDL。每用例一个 `tmp_path` 库文件，所以用例之间不会互相漏行
（PG 侧栽过那个坑，见 `test_pg_profile_store.py` 的 `_psycopg_dsn`）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.memory.profile_sqlite import SqliteMemoryProfileStore
from memory2.store import SCHEMA
from tests.memory.profile_store_cases import TENANT_A, CASES

_PROFILE_TABLES = ("tenant_memory_profile_sections", "tenant_memory_profile_entries")
_BASE_TS = datetime(2026, 10, 6, 1, 0, 0, tzinfo=timezone.utc)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def create_sqlite_memory_db(path: Path) -> None:
    """按 `memory2/store.py` 的 `SCHEMA` 建出单机库，与进程真正会拿到的 DDL 同源。

    画像播种与快照解析两组用例都要一个「表已就绪的空库」，这里只有一份建表动作。
    """
    conn = _connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "memory2.db"
    create_sqlite_memory_db(path)
    return path


@pytest.fixture()
def store(db_path: Path) -> Iterator[SqliteMemoryProfileStore]:
    instance = SqliteMemoryProfileStore(db_path, TENANT_A)
    yield instance
    instance.close()


class _SqliteHarness:
    """`ProfileStoreHarness` 的 SQLite 侧实现。

    `borrow` 开的是**另一条**连接：单机下解析器也是自己借一条连接，把 persona 的 SELECT
    和 `read_assembly` 都压在同一条上，所以这里不能图省事交回 `store` 自己那条。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._extra: list[sqlite3.Connection] = []
        self._stores: list[SqliteMemoryProfileStore] = []

    def close(self) -> None:
        for instance in self._stores:
            instance.close()
        for conn in self._extra:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = _connect(self._path)
        self._extra.append(conn)
        return conn

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[sqlite3.Connection]:
        yield self._conn()

    def borrow(self, store: Any) -> Any:
        return self._session()

    def store_for(self, tenant_id: str) -> SqliteMemoryProfileStore:
        instance = SqliteMemoryProfileStore(self._path, tenant_id)
        self._stores.append(instance)
        return instance

    async def count_rows(self, store: Any, table: str, category: str) -> int:
        if table not in _PROFILE_TABLES:
            raise AssertionError(f"不是画像专表：{table}")
        row = self._conn().execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = :t AND category = :c",
            {"t": store.tenant_id, "c": category},
        ).fetchone()
        return int(row[0])

    async def insert_without_happened_at(self, store: Any) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO tenant_memory_profile_entries "
            "(tenant_id, category, content) VALUES (:t, 'journal', 'x')",
            {"t": store.tenant_id},
        )
        conn.commit()

    async def commit(self, session: Any) -> None:
        session.commit()

    async def abort(self, session: Any) -> None:
        session.rollback()


@pytest.fixture()
def harness(db_path: Path) -> Iterator[_SqliteHarness]:
    instance = _SqliteHarness(db_path)
    yield instance
    instance.close()


@pytest.mark.parametrize("case", CASES, ids=[case.__name__ for case in CASES])
async def test_sqlite_profile_store(
    case: Any, store: SqliteMemoryProfileStore, harness: _SqliteHarness
) -> None:
    await case(store, harness)


async def test_happened_at_is_stored_as_uniform_utc_iso(
    store: SqliteMemoryProfileStore,
) -> None:
    """排序靠字典序，而字典序只有在列内格式唯一时才等于时间序。

    微秒为 0 的时刻若省掉小数位，同一列里就会出现两种长度；本用例钉住补齐后的形状。
    """
    await store.append_entry("journal", "整点", happened_at=_BASE_TS)
    await store.append_entry("journal", "半点", happened_at=_BASE_TS + timedelta(minutes=30))
    rows = store._db.execute(  # noqa: SLF001
        "SELECT happened_at FROM tenant_memory_profile_entries "
        "WHERE tenant_id = :t ORDER BY happened_at",
        {"t": TENANT_A},
    ).fetchall()
    stamps = [str(row[0]) for row in rows]
    assert len({len(stamp) for stamp in stamps}) == 1, f"格式不唯一：{stamps}"
    assert stamps == sorted(stamps)
    assert stamps[0].endswith("+00:00")


async def test_concurrent_writes_from_threaded_calls_all_land(
    store: SqliteMemoryProfileStore, harness: _SqliteHarness
) -> None:
    """每个方法都经 `asyncio.to_thread` 落在工作线程上，而连接是共享的一条。

    少那把 RLock 的话，两个线程在同一 connection 上交错执行会随机抛错或静默丢行；这里用
    一次并发批量写入把兜住它的机制钉住。
    """
    await asyncio.gather(
        *(
            store.append_entry("pending", f"P{i}", happened_at=_BASE_TS + timedelta(seconds=i))
            for i in range(20)
        )
    )
    async with harness.borrow(store) as session:
        taken = await store.take_pending(session)
        await harness.commit(session)
    assert taken.split("\n\n") == [f"P{i}" for i in range(20)]


def test_transaction_leaves_nothing_when_the_body_raises(
    store: SqliteMemoryProfileStore,
) -> None:
    """同步事务入口：中途抛错就一条都不留。

    单机写缝在一次线程跳转里连写画像行与审计行（`bootstrap/persona.py::SqliteRelationshipIO`），
    靠的就是这条回滚；入口之所以是同步的，是因为 async 会把锁持在一次 `await` 上，而
    `to_thread` 之后另一个线程再拿同一把 RLock 会直接锁死。
    """
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            store.write_section_sync(db, "relationship_state", "半个写入")
            raise RuntimeError("boom")

    assert store.read_section_sync(store._db, "relationship_state") == ""  # noqa: SLF001
