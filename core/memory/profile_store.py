"""C18 记忆画像层的存储接口（c18-tenant-scoped-memory ADR-13 / ADR-14）。

画像层住两张专表，不住 `memory_items`：`tenant_memory_profile_sections` 是单行整块类
（每租户每类一行，写为 upsert），`tenant_memory_profile_entries` 是追加日志类（事件日期
占 `happened_at`，幂等靠 `(tenant_id, dedup_key)`）。接口按这两种行形状分两组。

文件时代的方法（`backup_long_term`、`snapshot_pending` 一族、`get_memory_context` 等）
不在这里出现，也不在新表上重新实现——它们要么零调用方，要么只在「一份文件」这个前提下
才有意义。

`relationship_state` 归这套存储，但读写路径是 persona 那条缝（ADR-9）：组装侧只经
`PersonaSnapshot.relationship_state` 读它，不直查本表。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

SECTION_CATEGORIES: tuple[str, ...] = (
    "long_term",
    "recent_context",
    "relationship_state",
)
ENTRY_CATEGORIES: tuple[str, ...] = ("pending", "history", "journal")

# 组装期只预取这两类日志；journal 没有组装期消费者（ADR-8）。
ASSEMBLY_ENTRY_CATEGORIES: tuple[str, ...] = ("pending", "history")

# 两种后端共用这一份 SQL 正文。`:name` 形式对 PG（SQLAlchemy text()）与 sqlite3 都成立。
SECTION_READ_SQL = (
    "SELECT category, content FROM tenant_memory_profile_sections "
    "WHERE tenant_id = :tenant_id"
)
ENTRY_ASSEMBLY_READ_SQL = (
    "SELECT category, content FROM tenant_memory_profile_entries "
    "WHERE tenant_id = :tenant_id AND category IN ('pending', 'history') "
    "ORDER BY category, happened_at, created_at"
)
# 单类日志按时间正序读全；read_section 直接复用 SECTION_READ_SQL 取回该租户最多三行再
# 挑一类，不另开第二条正文——两张表的读取语句在此模块里一共就这三条，两侧共用。
ENTRY_CATEGORY_READ_SQL = (
    "SELECT content FROM tenant_memory_profile_entries "
    "WHERE tenant_id = :tenant_id AND category = :category "
    "ORDER BY happened_at, created_at"
)

_ENTRY_JOIN = "\n\n"


def render_entry_rows(rows: list[str], *, max_chars: int = 0) -> str:
    """把日志行拼成一段正文，超预算时取尾部。

    只有一份实现，两个后端共用，所以「history 取最近多少字符」不会在两侧各自演化。
    取尾部沿用文件版 `MemoryStore.read_history`（`agent/memory.py:100-109`）与 PG 版
    （`agent/memory_pg.py:88-104`）的既有语义：最新的条目在末尾，正因如此才不会被截掉。
    """
    text = _ENTRY_JOIN.join(rows)
    if max_chars > 0 and len(text) > max_chars:
        return text[-max_chars:]
    return text


@dataclass(frozen=True)
class ProfileAssembly:
    """一次会话借用取回的组装期内容。

    `relationship_state` 与四类画像同批返回，这才是 ADR-1 要的「同一瞬间」——它们出自
    同一条 SELECT，不存在 persona 与画像来自两个时刻的拼贴。
    """

    tenant_id: str
    long_term: str
    recent_context: str
    relationship_state: str
    pending: str
    history: str


@runtime_checkable
class MemoryProfileStoreApi(Protocol):
    """画像存储接口。两种后端各一个实现，全部 async。

    SQLite 侧底层是同步 `sqlite3`，照仓库既有先例用 `asyncio.to_thread` 包
    （`core/memory/markdown.py:783-785` 对 `read_history` 就是这么做的），不在协议层
    为两种后端分叉出两套签名。
    """

    async def read_section(self, category: str) -> str:
        """读单行整块类；该类没有行时返回空串——空语义，不是回退。"""
        ...

    async def write_section(self, category: str, content: str) -> None:
        """整行覆写。`relationship_state` 只允许 persona 那条缝调用。"""
        ...

    async def append_entry(
        self,
        category: str,
        content: str,
        *,
        happened_at: datetime,
        dedup_key: str | None = None,
    ) -> bool:
        """追加一行日志。

        `happened_at` 必填，日期占列由建表约束保证，不再靠实现记得填——`_append_entry`
        原来漏填，日期于是被拼进正文前缀。`dedup_key` 为 None 时不参与去重；给了键则
        同租户同键重复写入返回 False，不抛错也不产生第二行。
        """
        ...

    async def read_entries(self, category: str, *, max_chars: int = 0) -> str:
        """按时间顺序读某类日志并拼成一段正文。"""
        ...

    async def take_pending(self) -> str:
        """取走全部 pending 正文并在同一事务里删除。

        取代文件版的 `snapshot_pending` / `commit_pending_snapshot` /
        `rollback_pending_snapshot`：那边要靠原子重命名把内容先藏起来才能崩溃可恢复，
        这里回滚就是事务中止（ADR-14）。
        """
        ...

    async def read_assembly(
        self, session: Any, *, history_max_chars: int = 0
    ) -> ProfileAssembly:
        """在**调用方借好的会话**上跑两条 SELECT，返回组装期全部内容。

        为什么不自己开会话：ADR-1 要求 persona 与画像出自同一次借用。`session` 的合法
        取值随实现的后端而定——PG 侧是 `AsyncSession`，SQLite 侧是 `sqlite3.Connection`。
        两者没有共同接口（前者 `execute` 是协程、后者同步并返回 Cursor），所以只能收成
        `Any`。这是 ADR-14 明确接受的代价，兜住它的是 6.5 那条两侧共用一份参数化清单的
        用例。
        """
        ...
