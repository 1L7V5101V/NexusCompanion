from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import joinedload

from bootstrap.db.models.session import MessageModel, SessionModel


class AsyncSessionRepository:
    """Async CRUD for sessions and messages, scoped by tenant_id."""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    # ── sessions ──────────────────────────────────────────────

    async def get_session(self, tenant_id: str, key: str) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(SessionModel, (key,))
            if row is None or row.tenant_id != tenant_id:
                return None
            return _session_to_dict(row)

    async def session_exists(self, tenant_id: str, key: str) -> bool:
        return await self.get_session(tenant_id, key) is not None

    async def create_session(
        self,
        tenant_id: str,
        key: str,
        *,
        channel: str = "",
        chat_id: str = "",
        metadata: dict | None = None,
        last_user_at: str | None = None,
        last_proactive_at: str | None = None,
    ) -> dict | None:
        async with self._sf() as sess:
            now = datetime.now().astimezone()
            row = SessionModel(
                tenant_id=tenant_id,
                key=key,
                channel=channel,
                chat_id=chat_id,
                metadata_json=_to_json(metadata),
                last_user_at=_parse_dt(last_user_at),
                last_proactive_at=_parse_dt(last_proactive_at),
                created_at=now,
                updated_at=now,
            )
            sess.add(row)
            await sess.commit()
            await sess.refresh(row)
            return _session_to_dict(row)

    async def upsert_session(
        self,
        tenant_id: str,
        key: str,
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
        last_consolidated: int | None = None,
        metadata: dict | None = None,
    ) -> bool:
        async with self._sf() as sess:
            existing = await sess.get(SessionModel, (key,))
            if existing is None or existing.tenant_id != tenant_id:
                return False
            if created_at is not None:
                existing.created_at = _parse_dt(created_at)
            if updated_at is not None:
                existing.updated_at = _parse_dt(updated_at)
            if last_consolidated is not None:
                existing.last_consolidated = last_consolidated
            if metadata is not None:
                existing.metadata_json = _to_json(metadata)
            await sess.commit()
            return True

    async def update_session(
        self,
        tenant_id: str,
        key: str,
        *,
        metadata: dict | None = None,
        last_consolidated: int | None = None,
        last_user_at: str | None = None,
        last_proactive_at: str | None = None,
    ) -> dict | None:
        async with self._sf() as sess:
            existing = await sess.get(SessionModel, (key,))
            if existing is None or existing.tenant_id != tenant_id:
                return None
            if metadata is not None:
                existing.metadata_json = _to_json(metadata)
            if last_consolidated is not None:
                existing.last_consolidated = last_consolidated
            if last_user_at is not None:
                existing.last_user_at = _parse_dt(last_user_at)
            if last_proactive_at is not None:
                existing.last_proactive_at = _parse_dt(last_proactive_at)
            existing.updated_at = datetime.now().astimezone()
            await sess.commit()
            await sess.refresh(existing)
            return _session_to_dict(existing)

    async def delete_session(self, tenant_id: str, key: str, *, cascade: bool = False) -> bool:
        async with self._sf() as sess:
            existing = await sess.get(SessionModel, (key,))
            if existing is None or existing.tenant_id != tenant_id:
                return False
            if cascade:
                await sess.execute(
                    delete(MessageModel).where(
                        MessageModel.tenant_id == tenant_id,
                        MessageModel.session_key == key,
                    )
                )
            await sess.delete(existing)
            await sess.commit()
            return True

    async def delete_sessions_batch(self, tenant_id: str, keys: list[str], *, cascade: bool = False) -> int:
        async with self._sf() as sess:
            deleted = 0
            for key in keys:
                existing = await sess.get(SessionModel, (key,))
                if existing is None or existing.tenant_id != tenant_id:
                    continue
                if cascade:
                    await sess.execute(
                        delete(MessageModel).where(
                            MessageModel.tenant_id == tenant_id,
                            MessageModel.session_key == key,
                        )
                    )
                await sess.delete(existing)
                deleted += 1
            await sess.commit()
            return deleted

    async def list_sessions(self, tenant_id: str) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(SessionModel)
                    .where(SessionModel.tenant_id == tenant_id)
                    .order_by(SessionModel.updated_at.desc())
                )
            ).scalars().all()
            return [_session_to_dict(r) for r in rows]

    async def list_sessions_for_dashboard(
        self,
        tenant_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        channel: str | None = None,
        q: str | None = None,
        updated_from: str | None = None,
        updated_to: str | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict], int]:
        async with self._sf() as sess:
            query = select(SessionModel).where(SessionModel.tenant_id == tenant_id)
            count_query = select(func.count(SessionModel.key)).where(SessionModel.tenant_id == tenant_id)

            if channel:
                query = query.where(SessionModel.channel == channel)
                count_query = count_query.where(SessionModel.channel == channel)
            if q:
                like = f"%{q}%"
                query = query.where(SessionModel.key.ilike(like))
                count_query = count_query.where(SessionModel.key.ilike(like))
            if updated_from:
                dt = _parse_dt(updated_from)
                query = query.where(SessionModel.updated_at >= dt)
                count_query = count_query.where(SessionModel.updated_at >= dt)
            if updated_to:
                dt = _parse_dt(updated_to)
                query = query.where(SessionModel.updated_at <= dt)
                count_query = count_query.where(SessionModel.updated_at <= dt)

            total = (await sess.execute(count_query)).scalar() or 0

            sort_col = getattr(SessionModel, sort_by, SessionModel.updated_at)
            order_fn = sort_col.desc if sort_order == "desc" else sort_col.asc
            query = query.order_by(order_fn()).offset((page - 1) * page_size).limit(page_size)

            rows = (await sess.execute(query)).scalars().all()
            return [_session_to_dict(r) for r in rows], total

    # ── messages ─────────────────────────────────────────────

    async def insert_message(
        self,
        tenant_id: str,
        session_key: str,
        *,
        message_id: str,
        seq: int,
        role: str,
        content: str | None,
        ts: str,
        tool_chain: str | None = None,
        extra: str | None = None,
    ) -> dict:
        async with self._sf() as sess:
            row = MessageModel(
                tenant_id=tenant_id,
                id=message_id,
                session_key=session_key,
                seq=seq,
                role=role,
                content=content,
                tool_chain=tool_chain,
                extra=extra,
                ts=_parse_dt(ts),
            )
            sess.add(row)
            await sess.commit()
            await sess.refresh(row)
            return _message_to_dict(row)

    async def get_message(self, tenant_id: str, message_id: str) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(MessageModel, (message_id,))
            if row is None or row.tenant_id != tenant_id:
                return None
            return _message_to_dict(row)

    async def update_message(
        self,
        tenant_id: str,
        message_id: str,
        *,
        role: str | None = None,
        content: str | None = None,
        tool_chain: str | None = None,
        extra: str | None = None,
        ts: str | None = None,
    ) -> dict | None:
        async with self._sf() as sess:
            existing = await sess.get(MessageModel, (message_id,))
            if existing is None or existing.tenant_id != tenant_id:
                return None
            if role is not None:
                existing.role = role
            if content is not None:
                existing.content = content
            if tool_chain is not None:
                existing.tool_chain = tool_chain
            if extra is not None:
                existing.extra = extra
            if ts is not None:
                existing.ts = _parse_dt(ts)
            await sess.commit()
            await sess.refresh(existing)
            return _message_to_dict(existing)

    async def delete_message(self, tenant_id: str, message_id: str) -> bool:
        async with self._sf() as sess:
            existing = await sess.get(MessageModel, (message_id,))
            if existing is None or existing.tenant_id != tenant_id:
                return False
            await sess.delete(existing)
            await sess.commit()
            return True

    async def fetch_session_messages(self, tenant_id: str, session_key: str) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(MessageModel)
                    .where(
                        MessageModel.tenant_id == tenant_id,
                        MessageModel.session_key == session_key,
                    )
                    .order_by(MessageModel.seq.asc())
                )
            ).scalars().all()
            return [_message_to_dict(r) for r in rows]

    async def count_messages(self, tenant_id: str, session_key: str) -> int:
        async with self._sf() as sess:
            result = await sess.execute(
                select(func.count(MessageModel.id)).where(
                    MessageModel.tenant_id == tenant_id,
                    MessageModel.session_key == session_key,
                )
            )
            return result.scalar() or 0

    async def search_messages(
        self,
        tenant_id: str,
        query: str,
        *,
        session_key: str | None = None,
        role: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        async with self._sf() as sess:
            q = select(MessageModel).where(
                MessageModel.tenant_id == tenant_id,
                MessageModel.content.ilike(f"%{query}%"),
            )
            cq = select(func.count(MessageModel.id)).where(
                MessageModel.tenant_id == tenant_id,
                MessageModel.content.ilike(f"%{query}%"),
            )
            if session_key:
                q = q.where(MessageModel.session_key == session_key)
                cq = cq.where(MessageModel.session_key == session_key)
            if role:
                q = q.where(MessageModel.role == role)
                cq = cq.where(MessageModel.role == role)
            total = (await sess.execute(cq)).scalar() or 0
            rows = (await sess.execute(q.order_by(MessageModel.ts.desc()).offset(offset).limit(limit))).scalars().all()
            return [_message_to_dict(r) for r in rows], total

    # ── next_seq ─────────────────────────────────────────────

    async def next_seq(self, tenant_id: str, session_key: str) -> int:
        async with self._sf() as sess:
            existing = await sess.get(SessionModel, (session_key,))
            if existing is None or existing.tenant_id != tenant_id:
                return 0
            seq = existing.next_seq
            existing.next_seq = seq + 1
            await sess.commit()
            return seq

    # ── presence ─────────────────────────────────────────────

    async def update_presence(
        self,
        tenant_id: str,
        key: str,
        *,
        last_user_at: str | None = None,
        last_proactive_at: str | None = None,
    ):
        async with self._sf() as sess:
            existing = await sess.get(SessionModel, (key,))
            if existing is None or existing.tenant_id != tenant_id:
                return
            if last_user_at is not None:
                existing.last_user_at = _parse_dt(last_user_at)
            if last_proactive_at is not None:
                existing.last_proactive_at = _parse_dt(last_proactive_at)
            await sess.commit()

    async def get_presence(self, tenant_id: str, key: str) -> dict | None:
        async with self._sf() as sess:
            existing = await sess.get(SessionModel, (key,))
            if existing is None or existing.tenant_id != tenant_id:
                return None
            return {
                "last_user_at": existing.last_user_at.isoformat() if existing.last_user_at else None,
                "last_proactive_at": existing.last_proactive_at.isoformat() if existing.last_proactive_at else None,
            }


# ── helpers ─────────────────────────────────────────────────

def _session_to_dict(row: SessionModel) -> dict:
    return {
        "key": row.key,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        "last_consolidated": row.last_consolidated,
        "last_user_at": row.last_user_at.isoformat() if row.last_user_at else None,
        "last_proactive_at": row.last_proactive_at.isoformat() if row.last_proactive_at else None,
        "next_seq": row.next_seq,
        "metadata": _from_json(row.metadata_json) or {},
    }


def _message_to_dict(row: MessageModel) -> dict:
    return {
        "id": row.id,
        "session_key": row.session_key,
        "seq": row.seq,
        "role": row.role,
        "content": row.content,
        "tool_chain": row.tool_chain,
        "extra": row.extra,
        "ts": row.ts.isoformat() if row.ts else "",
    }


def _to_json(data: Any) -> str | None:
    import json
    return json.dumps(data, ensure_ascii=False) if data is not None else None


def _from_json(s: str | None) -> Any:
    import json
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
