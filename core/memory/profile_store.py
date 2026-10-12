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

from collections.abc import Callable, Sequence
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
# 挑一类，不另开第二条正文。
#
# 下面六条是该两张表的全部 SQL 正文，读取三条 + 写入三条，两侧后端共用。`ON CONFLICT
# ... DO UPDATE/DO NOTHING`、`EXCLUDED` 与 `DELETE ... RETURNING` 在 PG 与 SQLite 3.35+
# 上写法一致，所以「幂等靠什么键」这种语义也只有一处定义。
#
# 两条 INSERT 显式给 `id` 而不靠 PG 的 `DEFAULT gen_random_uuid()`：SQLite 侧没有等价的
# 服务端默认值，共用正文就只能由调用方把行 id 一并交出。
ENTRY_CATEGORY_READ_SQL = (
    "SELECT content FROM tenant_memory_profile_entries "
    "WHERE tenant_id = :tenant_id AND category = :category "
    "ORDER BY happened_at, created_at"
)
SECTION_UPSERT_SQL = (
    "INSERT INTO tenant_memory_profile_sections "
    "(id, tenant_id, category, content, created_at, updated_at) "
    "VALUES (:id, :tenant_id, :category, :content, :created_at, :updated_at) "
    "ON CONFLICT (tenant_id, category) DO UPDATE SET "
    "content = EXCLUDED.content, updated_at = EXCLUDED.updated_at"
)
ENTRY_APPEND_SQL = (
    "INSERT INTO tenant_memory_profile_entries "
    "(id, tenant_id, category, content, happened_at, dedup_key, created_at) "
    "VALUES (:id, :tenant_id, :category, :content, :happened_at, :dedup_key, :created_at) "
    "ON CONFLICT (tenant_id, dedup_key) DO NOTHING"
)
ENTRY_WITH_IDS_SQL = (
    "SELECT id, content, happened_at FROM tenant_memory_profile_entries "
    "WHERE tenant_id = :tenant_id AND category = :category "
    "ORDER BY happened_at, created_at"
)
# 按主键批量删除：合并成功只该删「这次真吃进去的那些行」，不能顺手把合并窗口里新追加的
# 行一起带走。命名占位 `:e0, :e1, …` 对 PG 的 `text()` 与 sqlite3 都成立，所以正文仍然
# 只有一份。空列表不合法（PG 会报语法错），判空由两个实现各自在发语句之前做。


def entries_delete_sql(entry_ids: Sequence[str]) -> str:
    placeholders = ", ".join(f":e{index}" for index in range(len(entry_ids)))
    return (
        "DELETE FROM tenant_memory_profile_entries "
        f"WHERE tenant_id = :tenant_id AND id IN ({placeholders})"
    )


def entries_delete_params(tenant_id: str, entry_ids: Sequence[Any]) -> dict[str, Any]:
    """`entry_ids` 的元素类型由后端决定：PG 绑 `uuid.UUID`，SQLite 绑 str。"""
    return {
        "tenant_id": tenant_id,
        **{f"e{index}": value for index, value in enumerate(entry_ids)},
    }


# 播种用的存在性探针：只问「这一类有没有行」，不问内容。用它而不是
# `read_section(...) == ""`——空串既可能是没有行，也可能是被合法清空过，后者不该被
# 播种重新填回 config 值。
SECTION_EXISTS_SQL = (
    "SELECT 1 FROM tenant_memory_profile_sections "
    "WHERE tenant_id = :tenant_id AND category = :category"
)

_ENTRY_JOIN = "\n\n"


def require_section(category: str) -> None:
    """挡住「把整块类当日志追加」这类分组错误。放模块级是因为两侧都得有同一条。"""
    if category not in SECTION_CATEGORIES:
        raise ValueError(f"不是整块类画像：{category!r}（可用：{SECTION_CATEGORIES}）")


def require_entry(category: str) -> None:
    if category not in ENTRY_CATEGORIES:
        raise ValueError(f"不是日志类画像：{category!r}（可用：{ENTRY_CATEGORIES}）")


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


def pick_section(rows: Sequence[tuple[Any, Any]], category: str) -> str:
    """从该租户最多三行里挑一类。

    读取语句全仓库只有一条正文（`SECTION_READ_SQL`）：宁可多取两行，也不为每类各开一条
    SELECT，那样两侧就有九条正文要各自保持同步。
    """
    for name, content in rows:
        if str(name) == category:
            return str(content or "")
    return ""


@dataclass(frozen=True)
class ProfileEntry:
    """一行日志类画像，连主键一起交出。

    主键是给优化器用的：读完 pending 要发一次 LLM 合并，回来时窗口可能已经变了，只按
    category 删除会把没参与合并的新行一起带走。`id` 在 PG 侧由 `uuid.UUID` 转成 str、
    在 SQLite 侧本来就是 str，所以协议上是一种形状。
    """

    id: str
    content: str


def render_entries(rows: Sequence[ProfileEntry]) -> str:
    """`ENTRY_WITH_IDS_SQL` 已按事件日期正序排好，这里只负责拼接。"""
    return render_entry_rows([row.content for row in rows])


def pick_entries(rows: Sequence[tuple[Any, Any, Any]]) -> list[ProfileEntry]:
    """把 `ENTRY_WITH_IDS_SQL` 的 (id, content, happened_at) 行收成 `ProfileEntry`。"""
    return [ProfileEntry(id=str(row[0]), content=str(row[1] or "")) for row in rows]


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


def build_assembly(
    tenant_id: str,
    section_rows: Sequence[tuple[Any, Any]],
    entry_rows: Sequence[tuple[Any, Any]],
    *,
    history_max_chars: int = 0,
) -> ProfileAssembly:
    """把两条 SELECT 的行映射成组装期内容，两个后端共用。

    「哪一类进哪个字段」「journal 不进组装期」这两条规则只有一份。写在各自实现里
    迟早分叉，而分叉的表现是同一轮对话在单机与多租户下注入不同内容。
    """
    sections = {str(name): str(content or "") for name, content in section_rows}
    entries = [(str(name), str(content or "")) for name, content in entry_rows]
    return ProfileAssembly(
        tenant_id=tenant_id,
        long_term=sections.get("long_term", ""),
        recent_context=sections.get("recent_context", ""),
        relationship_state=sections.get("relationship_state", ""),
        pending=render_entry_rows(
            [content for name, content in entries if name == "pending"]
        ),
        history=render_entry_rows(
            [content for name, content in entries if name == "history"],
            max_chars=history_max_chars,
        ),
    )


@runtime_checkable
class MemoryProfileStoreApi(Protocol):
    """画像存储接口。两种后端各一个实现，全部 async。

    SQLite 侧底层是同步 `sqlite3`，照仓库既有先例用 `asyncio.to_thread` 包
    （`core/memory/profile_writer.py` 的 `ProfileFileMaintainer` 对文件 store 的同步
    方法就是这么包的），不在协议层为两种后端分叉出两套签名。
    """

    async def read_section(self, category: str) -> str:
        """读单行整块类；该类没有行时返回空串——空语义，不是回退。"""
        ...

    async def write_section(
        self, category: str, content: str, *, session: Any | None = None
    ) -> None:
        """整行覆写。`relationship_state` 只允许 persona 那条缝调用。

        `session` 给定时写进调用方的事务且**不提交**，省略时自开一次借用并提交。6.6 的
        「画像行与 `persona_audit_events` 同事务」靠前者。
        """
        ...

    async def append_entry(
        self,
        category: str,
        content: str,
        *,
        happened_at: datetime,
        dedup_key: str | None = None,
        session: Any | None = None,
    ) -> bool:
        """追加一行日志。

        `happened_at` 必填，日期占列由建表约束保证，不再靠实现记得填——`_append_entry`
        原来漏填，日期于是被拼进正文前缀。`dedup_key` 为 None 时不参与去重；给了键则
        同租户同键重复写入返回 False，不抛错也不产生第二行。

        `session` 的语义与 `write_section` 相同。
        """
        ...

    async def read_entries(self, category: str, *, max_chars: int = 0) -> str:
        """按时间顺序读某类日志并拼成一段正文。"""
        ...

    async def read_entries_with_ids(self, category: str) -> list[ProfileEntry]:
        """按事件日期正序读某类日志，连主键一起给（优化器要靠键区分新旧行）。"""
        ...

    async def merge_entries_into_section(
        self, entry_ids: Sequence[str], section_category: str, content: str
    ) -> int:
        """**一个事务内**删掉这批日志行并覆写某个整块类，返回删掉的行数。

        为什么这两个动作必须同批：优化器先读 pending、再发一次 LLM 合并、最后写回
        long_term。删除与写回若分两次提交，中间进程死掉就是「事实已从 pending 消失、
        合并结果却没落库」——永久丢失。文件版靠原子重命名（`snapshot_pending` /
        `commit_pending_snapshot` / `rollback_pending_snapshot` 三步）防这件事；表上不需要
        那套，因为一个事务的中止就是回滚（ADR-14）。

        为什么不把「取走」做成一条 `DELETE ... RETURNING` 横跨 LLM 调用：那要求事务在
        整个 LLM 等待期间开着，而 SQLite 侧的连接由一把 `threading.RLock` 守着，它只对
        属主线程可重入——async 事务把锁持在一次 `await` 上就会在下一个工作线程上等同一把
        锁，直接锁死。所以事务保持短，只包住「删除 + 写回」这两条语句（ADR-14 修订）。

        `entry_ids` 为空时不发起删除，只覆写该整块类，返回 0。
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


ProfileStoreResolver = Callable[[str], MemoryProfileStoreApi]
# 「给我一个租户 id，还我一个绑定该租户的存储」——装配根持有的是这个函数，不是某个 store。
# 定义放在 core 侧（不是 `bootstrap/profile_access.py`）：`core/memory` 不能反向 import
# bootstrap，而 maintenance 需要给这个形状署名。实现见 ADR-3/ADR-4。
