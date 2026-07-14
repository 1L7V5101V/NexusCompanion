from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.memory import (
    ConsolidationEventModel,
    MemoryItemModel,
    MemoryReplacementModel,
)


class AsyncMemoryRepository:
    """Async CRUD for memory tables, scoped by tenant_id."""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    # ── memory_items ─────────────────────────────────────────

    async def get_item(self, tenant_id: str, item_id: str) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(MemoryItemModel, (item_id,))
            if row is None or row.tenant_id != tenant_id:
                return None
            return _memory_item_to_dict(row)

    async def upsert_item(
        self,
        tenant_id: str,
        item_id: str,
        *,
        memory_type: str,
        summary: str,
        content_hash: str,
        embedding: str | None = None,
        reinforcement: int = 1,
        emotional_weight: int = 0,
        extra_json: str | None = None,
        source_ref: str | None = None,
        happened_at: str | None = None,
        status: str = "active",
    ) -> dict:
        async with self._sf() as sess:
            existing = await sess.get(MemoryItemModel, (item_id,))
            now = datetime.now().astimezone()
            if existing is not None and existing.tenant_id == tenant_id:
                existing.memory_type = memory_type
                existing.summary = summary
                existing.content_hash = content_hash
                if embedding is not None:
                    existing.embedding = embedding
                existing.reinforcement = reinforcement
                existing.emotional_weight = emotional_weight
                if extra_json is not None:
                    existing.extra_json = extra_json
                if source_ref is not None:
                    existing.source_ref = source_ref
                if happened_at is not None:
                    existing.happened_at = _parse_dt(happened_at)
                existing.status = status
                existing.updated_at = now
                await sess.commit()
                await sess.refresh(existing)
                return _memory_item_to_dict(existing)
            else:
                row = MemoryItemModel(
                    tenant_id=tenant_id,
                    id=item_id,
                    memory_type=memory_type,
                    summary=summary,
                    content_hash=content_hash,
                    embedding=embedding,
                    reinforcement=reinforcement,
                    emotional_weight=emotional_weight,
                    extra_json=extra_json,
                    source_ref=source_ref,
                    happened_at=_parse_dt(happened_at),
                    status=status,
                    created_at=now,
                    updated_at=now,
                )
                sess.add(row)
                await sess.commit()
                await sess.refresh(row)
                return _memory_item_to_dict(row)

    async def delete_item(self, tenant_id: str, item_id: str) -> bool:
        async with self._sf() as sess:
            existing = await sess.get(MemoryItemModel, (item_id,))
            if existing is None or existing.tenant_id != tenant_id:
                return False
            await sess.delete(existing)
            await sess.commit()
            return True

    async def delete_items_batch(self, tenant_id: str, item_ids: list[str]) -> int:
        async with self._sf() as sess:
            result = await sess.execute(
                delete(MemoryItemModel).where(
                    MemoryItemModel.tenant_id == tenant_id,
                    MemoryItemModel.id.in_(item_ids),
                )
            )
            await sess.commit()
            return result.rowcount

    async def list_items_for_dashboard(
        self,
        tenant_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        memory_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict], int]:
        async with self._sf() as sess:
            query = select(MemoryItemModel).where(MemoryItemModel.tenant_id == tenant_id)
            count_query = select(func.count(MemoryItemModel.id)).where(MemoryItemModel.tenant_id == tenant_id)

            if memory_type:
                query = query.where(MemoryItemModel.memory_type == memory_type)
                count_query = count_query.where(MemoryItemModel.memory_type == memory_type)
            if status:
                query = query.where(MemoryItemModel.status == status)
                count_query = count_query.where(MemoryItemModel.status == status)
            if q:
                like = f"%{q}%"
                query = query.where(MemoryItemModel.summary.ilike(like))
                count_query = count_query.where(MemoryItemModel.summary.ilike(like))

            total = (await sess.execute(count_query)).scalar() or 0

            sort_col = getattr(MemoryItemModel, sort_by, MemoryItemModel.updated_at)
            order_fn = sort_col.desc if sort_order == "desc" else sort_col.asc
            query = query.order_by(order_fn()).offset((page - 1) * page_size).limit(page_size)

            rows = (await sess.execute(query)).scalars().all()
            return [_memory_item_to_dict(r) for r in rows], total

    async def list_by_type(self, tenant_id: str, memory_type: str) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(MemoryItemModel)
                    .where(
                        MemoryItemModel.tenant_id == tenant_id,
                        MemoryItemModel.memory_type == memory_type,
                    )
                    .order_by(MemoryItemModel.created_at.desc())
                )
            ).scalars().all()
            return [_memory_item_to_dict(r) for r in rows]

    # ── consolidation_events ─────────────────────────────────

    async def upsert_consolidation_event(
        self,
        tenant_id: str,
        source_ref: str,
        *,
        item_id: str | None = None,
    ) -> dict:
        async with self._sf() as sess:
            existing = await sess.get(ConsolidationEventModel, (source_ref,))
            now = datetime.now().astimezone()
            if existing is not None and existing.tenant_id == tenant_id:
                existing.item_id = item_id
                await sess.commit()
                await sess.refresh(existing)
                return _consolidation_event_to_dict(existing)
            else:
                row = ConsolidationEventModel(
                    tenant_id=tenant_id,
                    source_ref=source_ref,
                    item_id=item_id,
                    created_at=now,
                )
                sess.add(row)
                await sess.commit()
                await sess.refresh(row)
                return _consolidation_event_to_dict(row)

    async def list_events_by_time_range(
        self,
        tenant_id: str,
        *,
        start: str,
        end: str,
    ) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(ConsolidationEventModel)
                    .where(
                        ConsolidationEventModel.tenant_id == tenant_id,
                        ConsolidationEventModel.created_at.between(_parse_dt(start), _parse_dt(end)),
                    )
                    .order_by(ConsolidationEventModel.created_at.asc())
                )
            ).scalars().all()
            return [_consolidation_event_to_dict(r) for r in rows]

    # ── memory_replacements ──────────────────────────────────

    async def record_replacement(self, tenant_id: str, **kwargs: Any) -> dict:
        async with self._sf() as sess:
            row = MemoryReplacementModel(tenant_id=tenant_id, created_at=datetime.now().astimezone(), **kwargs)
            sess.add(row)
            await sess.commit()
            await sess.refresh(row)
            return _replacement_to_dict(row)

    # ── vector search (pgvector with Python-side fallback) ────

    async def vector_search(
        self,
        tenant_id: str,
        embedding_str: str,
        *,
        k: int = 10,
        memory_type: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Cosine-similarity search.

        Attempts pgvector ``<=>`` operator first.  If pgvector is not
        installed, falls back to an in-Python full-scan cosine similarity
        (slower but still works for small to medium datasets).
        """
        try:
            return await self._vector_search_pgvector(
                tenant_id, embedding_str, k=k,
                memory_type=memory_type, status=status,
            )
        except Exception:
            return await self._vector_search_python(
                tenant_id, embedding_str, k=k,
                memory_type=memory_type, status=status,
            )

    async def _vector_search_pgvector(
        self,
        tenant_id: str,
        embedding_str: str,
        *,
        k: int = 10,
        memory_type: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Vector search using the pgvector <=> (cosine distance) operator."""
        async with self._sf() as sess:
            embedding_list = [float(x) for x in embedding_str.strip("[]").split(",") if x.strip()]
            dim = len(embedding_list)
            embedding_literal = "[" + ",".join(str(v) for v in embedding_list) + "]"

            conditions = [f"mi.tenant_id = :tenant_id"]
            params: dict[str, Any] = {"tenant_id": tenant_id, "k": k}

            if memory_type:
                conditions.append("mi.memory_type = :memory_type")
                params["memory_type"] = memory_type
            if status:
                conditions.append("mi.status = :status")
                params["status"] = status

            where_clause = " AND ".join(conditions)

            sql = text(f"""
                SELECT mi.id, mi.memory_type, mi.summary, mi.content_hash,
                       mi.reinforcement, mi.emotional_weight, mi.extra_json,
                       mi.source_ref, mi.happened_at, mi.status,
                       mi.created_at, mi.updated_at,
                       (mi.embedding::vector(:dim) <=> :embedding::vector(:dim)) AS distance
                FROM memory_items mi
                WHERE {where_clause}
                ORDER BY distance ASC
                LIMIT :k
            """)

            rows_raw = await sess.execute(sql, {"dim": dim, "embedding": embedding_literal, **params})
            results = []
            for row in rows_raw:
                d = dict(row._mapping)
                d["distance"] = float(d["distance"]) if d.get("distance") is not None else 1.0
                if "embedding" in d:
                    del d["embedding"]
                results.append(d)
            return results

    async def _vector_search_python(
        self,
        tenant_id: str,
        embedding_str: str,
        *,
        k: int = 10,
        memory_type: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """In-Python full-scan cosine similarity fallback."""
        query_vec = _parse_embedding(embedding_str)
        if not query_vec:
            return []

        async with self._sf() as sess:
            query = select(MemoryItemModel).where(MemoryItemModel.tenant_id == tenant_id)
            if memory_type:
                query = query.where(MemoryItemModel.memory_type == memory_type)
            if status:
                query = query.where(MemoryItemModel.status == status)

            all_rows = (await sess.execute(query)).scalars().all()

        scored = []
        for row in all_rows:
            if not row.embedding:
                continue
            row_vec = _parse_embedding(row.embedding)
            if not row_vec:
                continue
            sim = _cosine_similarity(query_vec, row_vec)
            scored.append((sim, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, row in scored[:k]:
            d = _memory_item_to_dict(row)
            d["distance"] = 1.0 - sim
            results.append(d)
        return results


# ── helpers ─────────────────────────────────────────────────

def _memory_item_to_dict(row: MemoryItemModel) -> dict:
    return {
        "id": row.id,
        "memory_type": row.memory_type,
        "summary": row.summary,
        "content_hash": row.content_hash,
        "embedding": row.embedding,
        "reinforcement": row.reinforcement,
        "emotional_weight": row.emotional_weight,
        "extra_json": _from_json(row.extra_json),
        "source_ref": row.source_ref,
        "happened_at": row.happened_at.isoformat() if row.happened_at else None,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def _consolidation_event_to_dict(row: ConsolidationEventModel) -> dict:
    return {
        "source_ref": row.source_ref,
        "item_id": row.item_id,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _replacement_to_dict(row: MemoryReplacementModel) -> dict:
    return {
        "id": row.id,
        "old_item_id": row.old_item_id,
        "old_summary": row.old_summary,
        "new_item_id": row.new_item_id,
        "new_summary": row.new_summary,
        "relation_type": row.relation_type,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _parse_embedding(s: str) -> list[float] | None:
    """Parse an embedding string like '[0.1,0.2,0.3]' into a float list."""
    import json
    if not s:
        return None
    s = s.strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return [float(x) for x in s.strip("[]").split(",") if x.strip()]
    except (ValueError, TypeError):
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(av * bv for av, bv in zip(a, b, strict=False))
    na = sum(av * av for av in a) ** 0.5
    nb = sum(bv * bv for bv in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


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
