"""整理与压缩写入侧用例的共用件（c18 task 6.12 / 4.3）。

`profile_store_cases.py` 那份清单只管存储接口本身；这里管的是**装配形状**：一个按租户
解析 store 的解析器、一个会记下收到过哪些提示的假 provider、一个够长的会话。两个用例文件
（`test_consolidation_writes_profile_tables.py`、`test_background_write_failclosed.py`）
共用这些件，断言各自写。

两个租户是照 PG 的形状写的：真实的 SQLite 装配只承认 `DEFAULT_TENANT`（单机没有第二个
租户），要验「写不进别人的桶」就得自建解析器，而不是去放宽那条限制。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from core.memory.markdown import (
    MarkdownMemoryMaintenance,
)
from agent.memory import MemoryStore
from core.memory.profile_sqlite import SqliteMemoryProfileStore
from tests.memory.test_sqlite_profile_store import create_sqlite_memory_db

TENANT_A = "c18-write-a"
TENANT_B = "c18-write-b"
HISTORY_ENTRY = "[2026-03-15 10:00] 用户聊了 Zigbee 方案"


class TenantStores:
    """`ProfileStoreResolver` 的测试实现：按租户给一个绑定该租户的 SQLite store。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._stores: dict[str, SqliteMemoryProfileStore] = {}

    @classmethod
    def create(cls, tmp_path: Path) -> "TenantStores":
        db = tmp_path / "memory2.db"
        create_sqlite_memory_db(db)
        return cls(db)

    def resolve(self, tenant_id: str) -> SqliteMemoryProfileStore:
        if not str(tenant_id or "").strip():
            raise ValueError("缺少 tenant_id，拒绝解析画像存储")
        store = self._stores.get(tenant_id)
        if store is None:
            store = SqliteMemoryProfileStore(self._db_path, tenant_id)
            self._stores[tenant_id] = store
        return store

    def count_rows(self, tenant_id: str, table: str, category: str = "") -> int:
        """裸 SQL 数行：断言「落了几个桶」不能借被验对象自己的读法来数。"""
        conn = sqlite3.connect(str(self._db_path))
        try:
            if category:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ? AND category = ?",
                    (tenant_id, category),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def close(self) -> None:
        for store in self._stores.values():
            store.close()
        self._stores.clear()


def consolidation_provider() -> SimpleNamespace:
    """假 provider：只认两类提示词，并把收到过的正文留在 `prompts` 里。"""
    prompts: list[str] = []

    async def _chat(**kwargs: Any) -> SimpleNamespace:
        text = "\n".join(
            str(msg.get("content") or "") for msg in kwargs.get("messages", [])
        )
        prompts.append(text)
        if "近期语境压缩代理" in text:
            payload = (
                '{"active_topics":["用户最近关注 Zigbee 方案"],"user_preferences":[],'
                '"follow_ups":[],"avoidances":[],"ongoing_threads":[]}'
            )
        else:
            payload = (
                '{"history_entries":[{"summary":"'
                + HISTORY_ENTRY
                + '","emotional_weight":6}],'
                '"pending_items":[{"tag":"preference","content":"用户偏好本地控制"}]}'
            )
        return SimpleNamespace(content=payload)

    provider = SimpleNamespace(chat=AsyncMock(side_effect=_chat))
    provider.prompts = prompts  # type: ignore[attr-defined]
    return provider


def consolidation_session() -> SimpleNamespace:
    return SimpleNamespace(
        key="cli:1",
        messages=[
            {
                "role": "user",
                "content": "我买了 Zigbee 网关",
                "timestamp": "2026-03-15T10:00:00",
            },
            {
                "role": "assistant",
                "content": "记住了",
                "timestamp": "2026-03-15T10:01:00",
            },
        ],
        last_consolidated=0,
        _channel="cli",
        _chat_id="1",
    )


def file_backed_maintenance(
    workspace: Path,
    provider: SimpleNamespace | None = None,
) -> MarkdownMemoryMaintenance:
    """不绑解析器的 maintenance：文件 store 仍在构造签名里，6.10 才摘掉它。"""
    return MarkdownMemoryMaintenance(
        store=MemoryStore(workspace),
        provider=cast(Any, provider or consolidation_provider()),
        model="m",
        keep_count=20,
    )
