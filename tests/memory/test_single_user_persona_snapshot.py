"""c18 task 6.8：单机也装 persona 快照解析器与关系状态写缝。

`resolve_persona_snapshot` 过去只在 PG durable 装配，单机恒无快照，prompt 各块因此一直走
共享文件回退。本文件钉的是 SQLite 侧装上去的那个 callable 交出什么：与 PG 同形的一对快照、
三字段来自播种行、写缝演进的值下一次就读得到。

被测对象是解析器而不是整个 app：装配段要在真机启动里才有意义，而这里要防的回归是「单机
 resolver 交出 None → 各块静默回退共享文件」。

夹具与 config 从 `test_single_user_persona_seed.py` 导入（同一个 tmp_path 建库夹具，不必
再抄一遍）；无 PG 依赖，任何环境都真跑。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bootstrap.persona import (
    SqlitePersonaResolver,
    SqliteRelationshipIO,
    ensure_single_user_persona_rows,
)
from core.memory.profile_sqlite import SqliteMemoryProfileStore
from infra.storage.tenancy import DEFAULT_TENANT
from tests.memory.test_single_user_persona_seed import _CONFIG, memory_db

_TS = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)


async def test_sqlite_resolver_returns_the_pair_from_seeded_rows(
    memory_db: Path,
) -> None:
    ensure_single_user_persona_rows(memory_db, _CONFIG)
    store = SqliteMemoryProfileStore(memory_db)
    try:
        await store.write_section("long_term", "长期画像")
        await store.append_entry("pending", "待合并", happened_at=_TS)

        resolved = await SqlitePersonaResolver(store)(DEFAULT_TENANT)
        assert resolved is not None
        persona, profile = resolved

        assert persona.tenant_id == DEFAULT_TENANT
        assert persona.identity == "我是 Nexus"
        assert persona.personality_rules == "温和而具体"
        assert persona.relationship_state == "config 里的自模型"
        assert profile.long_term == "长期画像"
        assert profile.pending == "待合并"
        assert profile.recent_context == ""
    finally:
        store.close()


async def test_writeseam_value_flows_into_next_snapshot(memory_db: Path) -> None:
    """seed 值不冒充当前值：写缝演进之后，下一次解析读到的是新值。"""
    ensure_single_user_persona_rows(memory_db, _CONFIG)
    store = SqliteMemoryProfileStore(memory_db)
    try:
        await SqliteRelationshipIO(store).write(DEFAULT_TENANT, "演化后的当前值")
        resolved = await SqlitePersonaResolver(store)(DEFAULT_TENANT)
        assert resolved is not None
        assert resolved[0].relationship_state == "演化后的当前值"
    finally:
        store.close()


async def test_resolver_without_seed_is_none(memory_db: Path) -> None:
    """没播种就没有 profile 行 → 返回 None，而不是造一个空身份。"""
    store = SqliteMemoryProfileStore(memory_db)
    try:
        assert await SqlitePersonaResolver(store)(DEFAULT_TENANT) is None
    finally:
        store.close()


async def test_foreign_tenant_fails_closed(memory_db: Path) -> None:
    """单机 store 只认 DEFAULT_TENANT；别的租户说明上游归属没接上，要抛不要静默写。"""
    store = SqliteMemoryProfileStore(memory_db)
    try:
        with pytest.raises(ValueError):
            await SqlitePersonaResolver(store)("telegram:123")
        with pytest.raises(ValueError):
            await SqliteRelationshipIO(store).write("telegram:123", "不该落到默认租户")
        assert await store.read_section("relationship_state") == ""
    finally:
        store.close()
