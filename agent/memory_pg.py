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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.memory import MemoryItemModel
from bootstrap.db.models.extras import AppConfigModel

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
