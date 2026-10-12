"""整理与压缩用到的画像读写，落在租户专表上（c18-tenant-scoped-memory task 6.12）。

原先这些读写走进程级共享的 markdown store：文件后端没有租户维度，所以「A 的整理把 B 的
长期画像合并掉」在结构上就可能发生。装配根改持 `ProfileStoreResolver`（ADR-3）之后，每轮
maintenance 按它真正服务的那个租户解析出一个 store，本类就是那轮用到的读写门面。

段名与语义沿用文件时代的那套名字，调用方式全是 async。日志类的幂等键由 `source_ref` 与正文
摘要一起推出：重放同一次 consolidation 不会多出一份（旧 `app_configs` 那套 128 字符全局键
正是 6.12 要替掉的东西，见 ADR-13）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timezone

from core.memory.profile_store import MemoryProfileStoreApi, ProfileEntry


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

    async def read_pending_rows(self) -> list[ProfileEntry]:
        """优化器要的那批待合并事实，连主键一起给（ADR-14 修订）。"""
        return await self._store.read_entries_with_ids("pending")

    async def merge_pending_into_long_term(
        self, entry_ids: Sequence[str], content: str
    ) -> int:
        """一个事务内「删掉这些 pending 行 + 覆写 long_term」，返回删掉的行数。"""
        return await self._store.merge_entries_into_section(
            entry_ids, "long_term", content
        )
