"""c18 task 6.7：单机启动播种 persona 行与关系状态行，且只在行不存在时写。

design ADR-10 要单机也走 persona 缝：缺这两行时 `PersonaSnapshot` 在 SQLite 下恒为 None，
prompt 各块就继续走单体文件回退——那正是本 change 要摘掉的东西。

主断言是「二次初始化不覆盖已演化的当前值」：播种若每次都覆写，用户与 optimizer 共同演化
出来的关系状态会在每次重启时被 config 打回原值。

文件位置从 tasks.md 写的 `tests/bootstrap/` 挪进本目录：那不是一个已存在的包，而
`tests/persona/` 的 conftest 挂着 autouse 的 PG 清理夹具，会把这两条纯 SQLite 用例在无
PG 的环境里整组跳过——播种用例必须随时都能跑。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.config_models import PersonaConfig
from bootstrap.persona import SqliteRelationshipIO, ensure_single_user_persona_rows
from core.memory.profile_sqlite import SqliteMemoryProfileStore
from infra.storage.tenancy import DEFAULT_TENANT
from tests.memory.test_sqlite_profile_store import create_sqlite_memory_db

_CONFIG = PersonaConfig(
    identity="我是 Nexus",
    personality_rules="温和而具体",
    self_model="config 里的自模型",
)


@pytest.fixture()
def memory_db(tmp_path: Path) -> Path:
    path = tmp_path / "memory2.db"
    create_sqlite_memory_db(path)
    return path


def _persona_row(path: Path) -> tuple[str, ...]:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT source, identity, personality_rules, self_model "
            "FROM tenant_persona_profiles WHERE tenant_id = ?",
            (DEFAULT_TENANT,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return tuple(str(item) for item in row)


async def test_seed_creates_both_rows(memory_db: Path) -> None:
    ensure_single_user_persona_rows(memory_db, _CONFIG)

    assert _persona_row(memory_db) == (
        "custom",
        "我是 Nexus",
        "温和而具体",
        "config 里的自模型",
    )
    store = SqliteMemoryProfileStore(memory_db)
    try:
        assert await store.read_section("relationship_state") == "config 里的自模型"
    finally:
        store.close()


async def test_reseed_keeps_the_evolved_state(memory_db: Path) -> None:
    ensure_single_user_persona_rows(memory_db, _CONFIG)

    store = SqliteMemoryProfileStore(memory_db)
    try:
        await SqliteRelationshipIO(store).write(DEFAULT_TENANT, "我们一起走过的三个月")
        ensure_single_user_persona_rows(
            memory_db,
            PersonaConfig(
                identity="换了身份",
                personality_rules="换了规则",
                self_model="换了自模型",
            ),
        )
        assert await store.read_section("relationship_state") == "我们一起走过的三个月"
    finally:
        store.close()

    # persona 行也是首次为准（C9：提交后固定），config 改了不回头覆盖。
    assert _persona_row(memory_db)[1] == "我是 Nexus"
