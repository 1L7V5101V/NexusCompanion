"""PG-backed implementation of the MemoryStore interface.

Replaces markdown-file-based MEMORY.md / HISTORY.md / SELF.md etc.
with PostgreSQL storage.  Uses the ``memory_items`` table with
different ``memory_type`` values for each logical file.

Memory type mapping:
  - ``long_term``     → formerly MEMORY.md
  - ``self``          → formerly SELF.md
  - ``pending``       → formerly PENDING.md
  - ``recent_context`` → formerly RECENT_CONTEXT.md
  - ``history``       → formerly HISTORY.md (each entry is a row)
  - ``journal``       → formerly journal/YYYY-MM-DD.md (each entry is a row)

Idempotent append (consolidation dedup) uses app_configs with
key = ``consolidation_writes:{source_ref}:{kind}``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, func, delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bootstrap.db.models.memory import MemoryItemModel
from bootstrap.db.models.extras import AppConfigModel
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

if TYPE_CHECKING:
    from agent.config_models import PersonaConfig

logger = logging.getLogger(__name__)

_JOURNAL_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")

_DEFAULT_SELF_MD = """# PgMemoryMarkdownStore — Self Model

## Personality & Image
- I am Nexus, a direct, warm, proactively thoughtful long-term partner.
"""


def get_default_self_md(persona: "PersonaConfig | None" = None) -> str:
    if persona and persona.self_model:
        return persona.self_model
    return _DEFAULT_SELF_MD


class PgMemoryMarkdownStore:
    """MemoryStore replacement backed by PostgreSQL (memory_items + app_configs)."""

    def __init__(self, session_factory: async_sessionmaker, tenant_id: str) -> None:
        self._sf = session_factory
        self.tenant_id = tenant_id

    # ── long-term memory ─────────────────────────────────────

    async def read_long_term(self) -> str:
        return await self._read_content("long_term")

    async def write_long_term(self, content: str) -> None:
        await self._upsert_content("long_term", content)

    # ── history ──────────────────────────────────────────────

    async def append_history(self, entry: str) -> None:
        text = (entry or "").strip()
        if not text:
            return
        await self._append_entry("history", text)

    async def append_history_once(
        self,
        entry: str,
        *,
        source_ref: str,
        kind: str = "history_entry",
    ) -> bool:
        text = (entry or "").strip()
        if not text:
            return False
        return await self._append_once_with_index("history", text, source_ref, kind)

    async def read_history(self, max_chars: int = 0) -> str:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(MemoryItemModel.summary)
                    .where(
                        MemoryItemModel.tenant_id == self.tenant_id,
                        MemoryItemModel.memory_type == "history",
                    )
                    .order_by(MemoryItemModel.created_at.asc())
                )
            ).scalars().all()
        text = "\n\n".join(rows)
        if max_chars > 0 and len(text) > max_chars:
            return text[-max_chars:]
        return text

    # ── journal ──────────────────────────────────────────────

    async def append_journal(
        self,
        date_str: str,
        entry: str,
        *,
        source_ref: str = "",
        kind: str = "journal",
    ) -> bool:
        date_str = date_str.strip()
        text = (entry or "").strip()
        if not _JOURNAL_DATE_RE.fullmatch(date_str) or not text:
            return False
        prefixed = f"**{date_str}** {text}"
        if source_ref:
            return await self._append_once_with_index("journal", prefixed, source_ref, kind)
        await self._append_entry("journal", prefixed)
        return True

    # ── recent context ───────────────────────────────────────

    async def read_recent_context(self) -> str:
        return await self._read_content("recent_context")

    async def write_recent_context(self, content: str) -> None:
        await self._upsert_content("recent_context", content)

    # ── SELF.md ──────────────────────────────────────────────

    async def read_self(self) -> str:
        return await self._read_content("self")

    async def write_self(self, content: str) -> None:
        await self._upsert_content("self", content)

    # ── pending facts ────────────────────────────────────────

    async def read_pending(self) -> str:
        return await self._read_content("pending")

    async def append_pending(self, facts: str) -> None:
        text = (facts or "").strip()
        if not text:
            return
        await self._append_entry("pending", text)

    async def append_pending_once(
        self,
        facts: str,
        *,
        source_ref: str,
        kind: str = "pending",
    ) -> bool:
        text = (facts or "").strip()
        if not text:
            return False
        return await self._append_once_with_index("pending", text, source_ref, kind)

    async def clear_pending(self) -> None:
        async with self._sf() as sess:
            await sess.execute(
                delete(MemoryItemModel).where(
                    MemoryItemModel.tenant_id == self.tenant_id,
                    MemoryItemModel.memory_type == "pending",
                )
            )
            await sess.commit()

    # ── snapshot / two-phase commit (simplified for PG) ──────

    async def snapshot_pending(self) -> None:
        """Snapshot pending content into a saved version (no-op for PG)."""
        pass

    async def commit_pending_snapshot(self) -> None:
        """Commit snapshot (no-op for PG)."""
        pass

    async def rollback_pending_snapshot(self) -> None:
        """Rollback snapshot (no-op for PG)."""
        pass

    # ── internals ────────────────────────────────────────────

    async def _read_content(self, memory_type: str) -> str:
        """Read the single-row content for a given memory type."""
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(MemoryItemModel.summary)
                    .where(
                        MemoryItemModel.tenant_id == self.tenant_id,
                        MemoryItemModel.memory_type == memory_type,
                    )
                    .order_by(MemoryItemModel.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return row or ""

    async def _upsert_content(self, memory_type: str, content: str) -> None:
        """Upsert a single-row content blob."""
        import uuid

        async with self._sf() as sess:
            existing = (
                await sess.execute(
                    select(MemoryItemModel)
                    .where(
                        MemoryItemModel.tenant_id == self.tenant_id,
                        MemoryItemModel.memory_type == memory_type,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if existing:
                existing.summary = content
                existing.updated_at = now
            else:
                sess.add(
                    MemoryItemModel(
                        tenant_id=self.tenant_id,
                        id=f"{memory_type}_{uuid.uuid4().hex[:12]}",
                        memory_type=memory_type,
                        summary=content,
                        content_hash=str(hash(content)),
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
            await sess.commit()

    async def _append_entry(self, memory_type: str, entry: str) -> None:
        """Append a multi-row entry."""
        import uuid

        now = datetime.now(timezone.utc)
        async with self._sf() as sess:
            sess.add(
                MemoryItemModel(
                    tenant_id=self.tenant_id,
                    id=f"{memory_type}_{uuid.uuid4().hex[:12]}",
                    memory_type=memory_type,
                    summary=entry,
                    content_hash=str(hash(entry)),
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            await sess.commit()

    async def _append_once_with_index(
        self,
        memory_type: str,
        entry: str,
        source_ref: str,
        kind: str,
    ) -> bool:
        """Idempotent append — skip if (source_ref, kind) already written."""
        dedup_key = f"consolidation_writes:{source_ref}:{kind}"
        async with self._sf() as sess:
            existing = await sess.get(AppConfigModel, (dedup_key,))
            if existing is not None:
                return False
            sess.add(
                AppConfigModel(
                    tenant_id=self.tenant_id,
                    key=dedup_key,
                    value_json=json.dumps({
                        "source_ref": source_ref,
                        "kind": kind,
                        "memory_type": memory_type,
                        "written_at": datetime.now(timezone.utc).isoformat(),
                    }),
                )
            )
            await sess.flush()
            import uuid

            now = datetime.now(timezone.utc)
            sess.add(
                MemoryItemModel(
                    tenant_id=self.tenant_id,
                    id=f"{memory_type}_{uuid.uuid4().hex[:12]}",
                    memory_type=memory_type,
                    summary=entry,
                    content_hash=str(hash(entry)),
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            await sess.commit()
        return True


class PgMemoryProfileStore:
    """画像专表的 PG 实现（c18 ADR-13 / ADR-14）。

    与同文件里那个 `PgMemoryMarkdownStore` 的区别不是换个表名：那个类是照文件版接口的
    形状填出来的，`pending` 三个方法是空实现、正文哈希用 `str(hash(...))`，而它压根没被
    实例化过，所以这些从来没被检查过。本类照两张表自己的行形状设计，幂等靠
    `(tenant_id, category)` 与 `(tenant_id, dedup_key)`，不需要正文哈希。

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
