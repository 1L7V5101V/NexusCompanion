"""画像专表的 PG 存储实现（c18-tenant-scoped-memory ADR-13 / ADR-14）。

本文件只剩 `PgMemoryProfileStore` 一个类。原先住在旁边的 `PgMemoryMarkdownStore` 是照
文件版接口形状填出来的 PG 版本：`pending` 那三个方法是空实现、正文哈希用 `str(hash(...))`
（按进程随机化，重放即失效），而且它从未被实例化过——所以这些从来没被检查过。c18 把画像
层挪到两张专表后，那个类整体删除（task 6.3），SQL 正文则统一上收到
`core/memory/profile_store.py`，两种后端共用同一份。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.memory.profile_store import (
    ENTRY_APPEND_SQL,
    ENTRY_ASSEMBLY_READ_SQL,
    ENTRY_CATEGORY_READ_SQL,
    ENTRY_WITH_IDS_SQL,
    SECTION_READ_SQL,
    SECTION_UPSERT_SQL,
    ProfileAssembly,
    ProfileEntry,
    build_assembly,
    entries_delete_params,
    entries_delete_sql,
    pick_entries,
    pick_section,
    render_entry_rows,
    require_entry,
    require_section,
)

logger = logging.getLogger(__name__)




class PgMemoryProfileStore:
    """画像专表的 PG 实现：按两张表自己的行形状设计。

    整块类靠 `ON CONFLICT (tenant_id, category)` 覆写不增行，日志类靠
    `(tenant_id, dedup_key)` 幂等，都不需要正文哈希。

    `read_assembly` 收调用方的会话而不自己开：ADR-1 要求 persona 与画像出自同一次借用。
    """

    def __init__(self, session_factory: async_sessionmaker, tenant_id: str) -> None:
        self._sf = session_factory
        self.tenant_id = tenant_id

    @asynccontextmanager
    async def _borrow(self, session: AsyncSession | None) -> AsyncIterator[AsyncSession]:
        """给了会话就用它、且不提交；没给就自开一次借用，正常退出时提交。

        「提交与否」只在这里决定一次，所以不会出现某个写方法忘了提交、或某个本该加入调用方
        事务的写把别人的事务一起提交掉。
        """
        if session is not None:
            yield session
            return
        async with self._sf() as sess:
            yield sess
            await sess.commit()

    async def read_section(self, category: str) -> str:
        require_section(category)
        async with self._sf() as sess:
            rows = (
                await sess.execute(text(SECTION_READ_SQL), {"tenant_id": self.tenant_id})
            ).all()
        return pick_section(rows, category)

    async def write_section(
        self, category: str, content: str, *, session: AsyncSession | None = None
    ) -> None:
        require_section(category)
        now = datetime.now(timezone.utc)
        async with self._borrow(session) as sess:
            await sess.execute(
                text(SECTION_UPSERT_SQL),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": self.tenant_id,
                    "category": category,
                    "content": content,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    async def append_entry(
        self,
        category: str,
        content: str,
        *,
        happened_at: datetime,
        dedup_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        require_entry(category)
        async with self._borrow(session) as sess:
            result = await sess.execute(
                text(ENTRY_APPEND_SQL),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": self.tenant_id,
                    "category": category,
                    "content": content,
                    "happened_at": happened_at,
                    "dedup_key": dedup_key,
                    "created_at": datetime.now(timezone.utc),
                },
            )
        # dedup_key 为 NULL 时 PG 视各行互不相等，永远不会冲突，所以恒为 True。
        return bool(result.rowcount and result.rowcount > 0)

    async def read_entries(self, category: str, *, max_chars: int = 0) -> str:
        require_entry(category)
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    text(ENTRY_CATEGORY_READ_SQL),
                    {"tenant_id": self.tenant_id, "category": category},
                )
            ).scalars().all()
        return render_entry_rows([str(item) for item in rows], max_chars=max_chars)

    async def read_entries_with_ids(self, category: str) -> list[ProfileEntry]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    text(ENTRY_WITH_IDS_SQL),
                    {"tenant_id": self.tenant_id, "category": category},
                )
            ).all()
        return pick_entries(rows)

    async def merge_entries_into_section(
        self, entry_ids: Sequence[str], section_category: str, content: str
    ) -> int:
        """「删掉这批行 + 覆写整块类」走同一个会话、同一次提交。

        不选 `DELETE ... RETURNING` 横跨 LLM 那笔大事务：那要求连接在整次合并等待期
        idle-in-transaction，而 SQLite 侧根本做不到（`RLock` 只对属主线程可重入）。
        判据见协议里的 `merge_entries_into_section`。
        """
        require_section(section_category)
        now = datetime.now(timezone.utc)
        async with self._borrow(None) as sess:
            deleted = 0
            if entry_ids:
                result = await sess.execute(
                    text(entries_delete_sql(entry_ids)),
                    entries_delete_params(
                        self.tenant_id,
                        [uuid.UUID(str(item)) for item in entry_ids],
                    ),
                )
                deleted = int(result.rowcount or 0)
            await sess.execute(
                text(SECTION_UPSERT_SQL),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": self.tenant_id,
                    "category": section_category,
                    "content": content,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return deleted

    async def read_assembly(
        self, session: AsyncSession, *, history_max_chars: int = 0
    ) -> ProfileAssembly:
        sections = (
            await session.execute(
                text(SECTION_READ_SQL), {"tenant_id": self.tenant_id}
            )
        ).all()
        entries = (
            await session.execute(
                text(ENTRY_ASSEMBLY_READ_SQL), {"tenant_id": self.tenant_id}
            )
        ).all()
        return build_assembly(
            self.tenant_id, sections, entries, history_max_chars=history_max_chars
        )
