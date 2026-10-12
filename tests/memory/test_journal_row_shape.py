"""c18 task 3.5：日志类画像的事件日期占 `happened_at` 列，不拼进正文。

文件时代那条缺陷在这里（`agent/memory_pg.py` 旧的 `_append_entry` 从来没填过
`happened_at`，日期于是被降级拼进正文前缀）。那个类和那条拼接一起随 task 6.3 删除，
本文件因此改成对**新存储**的形状断言：日期在列上、正文原样、排序按事件日期而不是插入顺序。

`happened_at NOT NULL` 这条建表约束本身由 `profile_store_cases.py` 里的
`case_happened_at_is_required_by_the_schema` 在两种后端上钉住；这里补的是「形状」那一半，
并且额外验 journal 特有的那条：同一天多条写入的顺序不该由墙上时钟决定。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.memory.profile_sqlite import SqliteMemoryProfileStore
from tests.memory.test_sqlite_profile_store import create_sqlite_memory_db

TENANT = "c18-journal-shape"


@pytest.fixture()
def store(tmp_path: Path):
    db = tmp_path / "memory2.db"
    create_sqlite_memory_db(db)
    instance = SqliteMemoryProfileStore(db, TENANT)
    try:
        yield instance
    finally:
        # `-W error` 下未关的 sqlite 连接会由 GC 在后面的用例里报 ResourceWarning。
        instance.close()


def _rows(db: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(db))
    try:
        return [
            (str(content), str(happened))
            for content, happened in conn.execute(
                "SELECT content, happened_at FROM tenant_memory_profile_entries "
                "WHERE tenant_id = ? AND category = 'journal' ORDER BY happened_at",
                (TENANT,),
            ).fetchall()
        ]
    finally:
        conn.close()


async def test_journal_date_lands_on_the_column_not_in_the_body(
    store: SqliteMemoryProfileStore, tmp_path: Path
) -> None:
    await store.append_entry(
        "journal",
        "[2026-03-08 12:00] 用户确认需求",
        happened_at=datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc),
    )

    body, happened = _rows(tmp_path / "memory2.db")[0]
    assert body == "[2026-03-08 12:00] 用户确认需求", "正文原样存，不额外拼日期前缀"
    assert not body.startswith("**"), f"日期被拼回正文了：{body!r}"
    assert happened.startswith("2026-03-08T00:00:00"), f"happened_at 没落对：{happened!r}"


async def test_entries_order_by_event_date_not_insertion_order(
    store: SqliteMemoryProfileStore,
) -> None:
    """先写后一天、再写前一天：读回来的顺序按事件日期，不按写入顺序。"""
    await store.append_entry(
        "journal", "三月的", happened_at=datetime(2026, 3, 20, tzinfo=timezone.utc)
    )
    await store.append_entry(
        "journal", "二月的", happened_at=datetime(2026, 2, 1, tzinfo=timezone.utc)
    )

    assert await store.read_entries("journal") == "二月的\n\n三月的"
