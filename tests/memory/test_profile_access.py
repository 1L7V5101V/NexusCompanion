"""c18 task 4.2 的解析器本身：租户 → 画像存储入口（design ADR-3 / ADR-4）。

装配根不再自己拿进程级共享 store，而是持这个解析器、在处理某个租户的数据时才解析。
本文件钉住三条会静默出错的行为：

- SQLite 那条必须复用同一个 store 实例：它自己开连接，每轮新建就是每轮多开一个文件句柄；
- 归属缺失或非固定租户要抛，不是悄悄落到 `default` 桶里——静默改桶等于把隔离换成猜测；
- PG 那条在配置说 postgres 却没给连接串时要在**装配期**就失败，而不是等第一次写入。

PG 分支不建连接也能验：`PgMemoryProfileStore` 的构造零 I/O，所以注入一个会话工厂即可检查
绑定关系，不必起库。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bootstrap.profile_access import build_profile_access
from core.memory.profile_sqlite import SqliteMemoryProfileStore
from infra.storage.tenancy import DEFAULT_TENANT
from tests.memory.test_sqlite_profile_store import create_sqlite_memory_db


def _config(backend: str, postgres_url: str = "") -> Any:
    return SimpleNamespace(storage=SimpleNamespace(backend=backend, postgres_url=postgres_url))


@pytest.fixture()
def memory_db(tmp_path: Path) -> Path:
    path = tmp_path / "memory2.db"
    create_sqlite_memory_db(path)
    return path


def test_sqlite_reuses_one_store_and_writes_land_in_tenant_bucket(memory_db: Path) -> None:
    access = build_profile_access(
        _config("sqlite"), memory_db_path=memory_db, session_factory=None
    )
    store = access.resolve(DEFAULT_TENANT)
    assert store is access.resolve(DEFAULT_TENANT), "每次新建就每次多开一个文件句柄"

    async def _write_read() -> str:
        await store.write_section("long_term", "解析器写的")
        return await store.read_section("long_term")

    assert asyncio.run(_write_read()) == "解析器写的"
    # 落的是带 tenant_id 的专表行，不是共享目录里的文件。
    conn = sqlite3.connect(str(memory_db))
    rows = conn.execute(
        "SELECT tenant_id, content FROM tenant_memory_profile_sections"
    ).fetchall()
    conn.close()
    assert rows == [(DEFAULT_TENANT, "解析器写的")]
    access.close()


def test_sqlite_refuses_other_tenants_and_empty_attribution(memory_db: Path) -> None:
    access = build_profile_access(
        _config("sqlite"), memory_db_path=memory_db, session_factory=None
    )
    with pytest.raises(ValueError):
        access.resolve("telegram:42")
    with pytest.raises(ValueError):
        access.resolve("")
    with pytest.raises(ValueError):
        access.resolve("   ")


def test_close_allows_a_fresh_store(memory_db: Path) -> None:
    access = build_profile_access(
        _config("sqlite"), memory_db_path=memory_db, session_factory=None
    )
    first = access.resolve(DEFAULT_TENANT)
    access.close()
    second = access.resolve(DEFAULT_TENANT)
    assert second is not first, "close 后缓存要清空，别再交给已关的连接"
    # 新建出来的这条也得关：`-W error` 下未关的 sqlite 连接会由 GC 在**后面某个用例**里抛出
    # ResourceWarning，报错算到不相干的用例头上（本 change 已经栽过一次）。
    access.close()


def test_postgres_fails_at_assembly_without_url() -> None:
    with pytest.raises(ValueError, match="postgres_url"):
        build_profile_access(_config("postgres", ""))


def test_postgres_store_is_bound_to_the_requested_tenant() -> None:
    from agent.memory_pg import PgMemoryProfileStore

    sentinel = object()
    access = build_profile_access(
        _config("postgres", "postgresql://x/y"), session_factory=sentinel
    )
    store = access.resolve("tenant-a")
    assert isinstance(store, PgMemoryProfileStore)
    assert store.tenant_id == "tenant-a"
    with pytest.raises(ValueError):
        access.resolve("")
    # 注入的工厂不属于本模块，close() 不该去 dispose 它。
    access.close()
