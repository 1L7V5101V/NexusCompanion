"""画像存储的跨后端用例清单（c18 task 6.4 / 6.5）。

同一份断言跑两个后端，是 ADR-14 那条「两侧不能各自漂移」的验收口径：清单只有一份，
每个后端只提供一件东西——`ProfileStoreHarness`，把「借一次会话」「开同存储上另一个租户
的实例」「跑裸 SQL」这三件后端特有的事包住。断言本身不认识后端。

驱动方式在 `test_pg_profile_store.py` 与 `test_sqlite_profile_store.py` 各自的文件里。

侧重点不是「能不能存进去」，而是 ADR-13 / ADR-14 换来的三条语义：整块类覆写不增行、
日志类靠 `(tenant_id, dedup_key)` 幂等且该键按租户隔离（F3）、pending 的取走与删除是一
个原子动作（取代文件版那套「原子重命名 + 提交/回滚」三步）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any, Protocol

import pytest

from core.memory.profile_store import MemoryProfileStoreApi

TENANT_A = "c18-store-a"
TENANT_B = "c18-store-b"
CLEANUP_PREFIX = "c18-store-"


def _ts(day: int) -> datetime:
    return datetime(2026, 10, day, 8, 0, 0, tzinfo=timezone.utc)


class ProfileStoreHarness(Protocol):
    """后端特有的事：借一条会话、决定那笔事务提交还是中止、按租户再开一个实例、跑裸 SQL。"""

    def borrow(self, store: Any) -> AbstractAsyncContextManager[Any]:
        """产出一个可交给 `read_assembly` 的会话句柄：PG 是 `AsyncSession`，
        SQLite 是 `sqlite3.Connection`。"""
        ...

    def store_for(self, tenant_id: str) -> Any:
        """同一个存储上的另一个租户实例。"""
        ...

    async def count_rows(self, store: Any, table: str, category: str) -> int:
        """该类在本租户下占了几行——整块类必须恒为 1。"""
        ...

    async def insert_without_happened_at(self, store: Any) -> None:
        """绕开接口插一行日志却不给 `happened_at`，建表约束应当把它挡下来。"""
        ...

    async def commit(self, session: Any) -> None:
        """提交借来的会话。`take_pending` 与带 `session` 的写都不自己提交。"""
        ...

    async def abort(self, session: Any) -> None:
        """中止借来的会话——ADR-14 那句「回滚就是事务中止」靠它验。"""
        ...


async def case_satisfies_the_protocol(store: Any, harness: ProfileStoreHarness) -> None:
    assert isinstance(store, MemoryProfileStoreApi)


async def case_section_is_one_row_per_category(
    store: Any, harness: ProfileStoreHarness
) -> None:
    await store.write_section("long_term", "第一版")
    assert await store.read_section("long_term") == "第一版"
    await store.write_section("long_term", "第二版")
    assert await store.read_section("long_term") == "第二版", "整块类必须覆写而不是追加"
    count = await harness.count_rows(store, "tenant_memory_profile_sections", "long_term")
    assert count == 1, "覆写没有留下第二行"


async def case_missing_section_is_empty_not_a_fallback(
    store: Any, harness: ProfileStoreHarness
) -> None:
    assert await store.read_section("recent_context") == ""


async def case_category_groups_are_enforced(
    store: Any, harness: ProfileStoreHarness
) -> None:
    with pytest.raises(ValueError):
        await store.write_section("self", "不在整块组里")
    with pytest.raises(ValueError):
        await store.append_entry("long_term", "整块类不能当日志追加", happened_at=_ts(1))


async def case_entries_read_in_event_order_and_tail_to_budget(
    store: Any, harness: ProfileStoreHarness
) -> None:
    for day in (3, 1, 2):
        await store.append_entry("history", f"E{day}", happened_at=_ts(day))
    assert await store.read_entries("history") == "E1\n\nE2\n\nE3"
    assert await store.read_entries("history", max_chars=5) == "2\n\nE3"


async def case_dedup_key_is_per_tenant(
    store: Any, harness: ProfileStoreHarness
) -> None:
    key = "consolidation:" + "m" * 400  # 远超原 app_configs.key String(128) 的长度
    assert await store.append_entry("history", "一", happened_at=_ts(1), dedup_key=key)
    assert not await store.append_entry(
        "history", "二", happened_at=_ts(1), dedup_key=key
    )
    assert await store.read_entries("history") == "一"
    other = harness.store_for(TENANT_B)
    assert await other.append_entry(
        "history", "三", happened_at=_ts(1), dedup_key=key
    ), "F3：租户 B 不该被租户 A 留下的同名键静默判重"
    assert await other.read_entries("history") == "三"


async def case_null_dedup_key_always_appends(
    store: Any, harness: ProfileStoreHarness
) -> None:
    assert await store.append_entry("pending", "a", happened_at=_ts(1))
    assert await store.append_entry("pending", "b", happened_at=_ts(2))
    assert await store.read_entries("pending") == "a\n\nb"


async def case_take_pending_returns_and_empties(
    store: Any, harness: ProfileStoreHarness
) -> None:
    await store.append_entry("pending", "事实二", happened_at=_ts(2))
    await store.append_entry("pending", "事实一", happened_at=_ts(1))
    async with harness.borrow(store) as session:
        taken = await store.take_pending(session)
        await harness.commit(session)
    assert taken == "事实一\n\n事实二"
    async with harness.borrow(store) as session:
        assert await store.take_pending(session) == ""
        await harness.commit(session)
    assert await store.read_entries("history") == "", "只该带走 pending"


async def case_take_pending_rolls_back_with_the_caller(
    store: Any, harness: ProfileStoreHarness
) -> None:
    """ADR-14 用「事务中止」取代文件版的原子重命名，这条就得真成立。

    6.11 的失败路径靠它：合并失败时 pending 必须原样在库里。`take_pending` 若自己提交，
    这里读到的就是空——那是比文件版更糟的结果，所以协议不给「不传会话」的退路。
    """
    await store.append_entry("pending", "留着", happened_at=_ts(1))
    async with harness.borrow(store) as session:
        assert await store.take_pending(session) == "留着"
        await harness.abort(session)
    assert await store.read_entries("pending") == "留着"


async def case_write_section_joins_the_caller_transaction(
    store: Any, harness: ProfileStoreHarness
) -> None:
    """6.6 的画像行 + 审计事件、6.11 的合并写回都要靠 `session=` 传进来。"""
    await store.write_section("long_term", "旧值")
    async with harness.borrow(store) as session:
        await store.write_section("long_term", "新值", session=session)
        await harness.abort(session)
    assert await store.read_section("long_term") == "旧值", "中止后不该留下半个覆写"

    async with harness.borrow(store) as session:
        await store.write_section("long_term", "合入值", session=session)
        await harness.commit(session)
    assert await store.read_section("long_term") == "合入值"


async def case_read_assembly_uses_the_borrowed_session(
    store: Any, harness: ProfileStoreHarness
) -> None:
    await store.write_section("long_term", "长期")
    await store.write_section("recent_context", "近期")
    await store.write_section("relationship_state", "关系状态")
    await store.append_entry("pending", "待合并", happened_at=_ts(1))
    await store.append_entry("history", "历史", happened_at=_ts(1))
    await store.append_entry("journal", "不该进组装期", happened_at=_ts(1))

    async with harness.borrow(store) as session:
        assembly = await store.read_assembly(session, history_max_chars=50)

    assert assembly.tenant_id == store.tenant_id
    assert assembly.long_term == "长期"
    assert assembly.recent_context == "近期"
    assert assembly.relationship_state == "关系状态", "ADR-1：与四类画像出自同一条 SELECT"
    assert assembly.pending == "待合并"
    assert assembly.history == "历史"
    assert "不该进组装期" not in (assembly.pending + assembly.history)


async def case_happened_at_is_required_by_the_schema(
    store: Any, harness: ProfileStoreHarness
) -> None:
    """ADR-8 的日期占列靠建表约束，不靠实现记得填——原缺口就是这么产生的。"""
    with pytest.raises(Exception):
        await harness.insert_without_happened_at(store)
    assert await store.read_entries("journal") == "", "上面的插入必须真的没落库"


CASES = (
    case_satisfies_the_protocol,
    case_section_is_one_row_per_category,
    case_missing_section_is_empty_not_a_fallback,
    case_category_groups_are_enforced,
    case_entries_read_in_event_order_and_tail_to_budget,
    case_dedup_key_is_per_tenant,
    case_null_dedup_key_always_appends,
    case_take_pending_returns_and_empties,
    case_take_pending_rolls_back_with_the_caller,
    case_write_section_joins_the_caller_transaction,
    case_read_assembly_uses_the_borrowed_session,
    case_happened_at_is_required_by_the_schema,
)
