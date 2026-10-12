"""整理与压缩用到的画像读写，落在租户专表上（c18-tenant-scoped-memory task 6.12）。

原先这些读写走进程级共享的 markdown store：文件后端没有租户维度，所以「A 的整理把 B 的
长期画像合并掉」在结构上就可能发生。装配根改持 `ProfileStoreResolver`（ADR-3）之后，每轮
maintenance 按它真正服务的那个租户解析出一个 store，本类就是那轮用到的读写门面。

方法名保持与 worker 原来对文件 store 的调用一致，这样 `_MarkdownConsolidationWorker` 里
只有「调用方式」要改（同步转 await），段名与语义不动。日志类的幂等键由 `source_ref` 与正文
摘要一起推出：重放同一次 consolidation 不会多出一份（旧 `app_configs` 那套 128 字符全局键
正是 6.12 要替掉的东西，见 ADR-13）。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.memory.profile_store import MemoryProfileStoreApi

if TYPE_CHECKING:
    from agent.memory import MemoryStore


@runtime_checkable
class ProfileMaintainer(Protocol):
    """整理与压缩那一轮用到的画像读写，统一的 async 形状。

    两个实现：`ProfileTableMaintainer`（租户专表）与 `ProfileFileMaintainer`（文件后端）。
    worker 只认这一种形状，「同步文件 IO 要挪进线程」这件事留在门面内部。
    """

    async def read_long_term(self) -> str: ...

    async def write_long_term(self, content: str) -> None: ...

    async def read_recent_context(self) -> str: ...

    async def write_recent_context(self, content: str) -> None: ...

    async def read_history(self, max_chars: int = 0) -> str: ...

    async def append_history_once(
        self, entry: str, *, source_ref: str, kind: str = "history_entry"
    ) -> bool: ...

    async def append_pending_once(
        self, facts: str, *, source_ref: str, kind: str = "pending_items"
    ) -> bool: ...

    async def append_journal(
        self,
        date_str: str,
        entry: str,
        *,
        source_ref: str = "",
        kind: str = "journal",
    ) -> bool: ...


def _dedup_key(kind: str, source_ref: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{source_ref}:sha256:{digest}"


def _journal_date(value: str) -> datetime:
    """journal 的日期来自 `YYYY-MM-DD` 文件名，占 `happened_at` 列而不是拼进正文。"""
    return datetime.fromisoformat(value.strip()).replace(tzinfo=timezone.utc)


class ProfileTableMaintainer:
    """一轮 maintenance 对一个租户的画像读写门面。方法全是 async，调用方直接 await。"""

    def __init__(self, store: MemoryProfileStoreApi, tenant_id: str) -> None:
        self._store = store
        self.tenant_id = tenant_id

    # ── 读侧（整理前的现状）──────────────────────────────────────────

    async def read_long_term(self) -> str:
        return await self._store.read_section("long_term")

    async def read_recent_context(self) -> str:
        return await self._store.read_section("recent_context")

    async def read_history(self, max_chars: int = 0) -> str:
        return await self._store.read_entries("history", max_chars=max_chars)

    # ── 写侧（原来落在 MEMORY.md / HISTORY.md / PENDING.md / journal/）──

    async def write_long_term(self, content: str) -> None:
        await self._store.write_section("long_term", content)

    async def write_recent_context(self, content: str) -> None:
        await self._store.write_section("recent_context", content)

    async def append_history_once(
        self, entry: str, *, source_ref: str, kind: str = "history_entry"
    ) -> bool:
        text = (entry or "").strip()
        if not text:
            return False
        return await self._store.append_entry(
            "history",
            text,
            happened_at=datetime.now(timezone.utc),
            dedup_key=_dedup_key(kind, source_ref, text),
        )

    async def append_pending_once(
        self, facts: str, *, source_ref: str, kind: str = "pending_items"
    ) -> bool:
        text = (facts or "").strip()
        if not text:
            return False
        return await self._store.append_entry(
            "pending",
            text,
            happened_at=datetime.now(timezone.utc),
            dedup_key=_dedup_key(kind, source_ref, text),
        )

    async def append_journal(
        self,
        date_str: str,
        entry: str,
        *,
        source_ref: str = "",
        kind: str = "journal",
    ) -> bool:
        """journal 逐行落表：文件名日期进 `happened_at`，正文原样（不拼 `**YYYY-MM-DD**` 前缀）。"""
        text = (entry or "").strip()
        if not text:
            return False
        return await self._store.append_entry(
            "journal",
            text,
            happened_at=_journal_date(date_str),
            dedup_key=_dedup_key(f"{kind}:{date_str}", source_ref, text),
        )

    async def take_pending(self, session: object) -> str:
        """优化器取走待合并事实；事务由调用方给（ADR-14）。"""
        return await self._store.take_pending(session)


class ProfileFileMaintainer:
    """`MemoryStore`（文件后端）的 async 门面，形状与 `ProfileTableMaintainer` 一致。

    只为一件事存在：装配根按租户解析专表（task 4.2）与摘掉文件后端（task 6.10）是两个
    步骤，中间这段时间 maintenance 仍要在没有解析器的构造点（评测脚本、直接构造的测试）
    写文件。task 6.10 删掉那条回退时，本类与它一起删除。
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    async def read_long_term(self) -> str:
        return str(await asyncio.to_thread(self._store.read_long_term) or "")

    async def write_long_term(self, content: str) -> None:
        await asyncio.to_thread(self._store.write_long_term, content)

    async def read_recent_context(self) -> str:
        return str(await asyncio.to_thread(self._store.read_recent_context) or "")

    async def write_recent_context(self, content: str) -> None:
        await asyncio.to_thread(self._store.write_recent_context, content)

    async def read_history(self, max_chars: int = 0) -> str:
        return str(
            await asyncio.to_thread(self._store.read_history, max_chars) or ""
        )

    async def append_history_once(
        self, entry: str, *, source_ref: str, kind: str = "history_entry"
    ) -> bool:
        return bool(
            await asyncio.to_thread(
                self._store.append_history_once,
                entry,
                source_ref=source_ref,
                kind=kind,
            )
        )

    async def append_pending_once(
        self, facts: str, *, source_ref: str, kind: str = "pending_items"
    ) -> bool:
        return bool(
            await asyncio.to_thread(
                self._store.append_pending_once,
                facts,
                source_ref=source_ref,
                kind=kind,
            )
        )

    async def append_journal(
        self,
        date_str: str,
        entry: str,
        *,
        source_ref: str = "",
        kind: str = "journal",
    ) -> bool:
        return bool(
            await asyncio.to_thread(
                self._store.append_journal,
                date_str,
                entry,
                source_ref=source_ref,
                kind=kind,
            )
        )
