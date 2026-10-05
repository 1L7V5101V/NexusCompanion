"""C9 persona 解析与 RelationshipState PG 写缝（c9-persona-relationship ADR-1/4/5）。

- `resolve_persona_snapshot`：turn 入口在 tenant lane 内调用一次，取该 tenant 的
  PersonaProfile（PG 固定快照）与 RelationshipState（`memory_items` self 当前值），
  组成不可变快照随 ContextRequest 走 prompt 组装。无 profile（onboarding 未完成）
  返回 None → prompt 各 block 走单体回退语义。
- `PgRelationshipIO`：optimizer 的 RelationshipState 读写落 PG seam——写为
  memory_items self 行 upsert + `persona_audit_events` 追加（同事务，ADR-5
  单写者语义：串行由调用方 tenant lock / lane 保证，这里保证原子与审计）。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.core.types import PersonaSnapshot


async def resolve_persona_snapshot(
    session_factory: async_sessionmaker, tenant_id: str
) -> PersonaSnapshot | None:
    """解析 tenant persona 快照；无 profile 行返回 None（fail-open 到单体回退）。"""
    async with session_factory() as sess:
        profile = (
            await sess.execute(
                text("""
                    SELECT source, identity, personality_rules
                    FROM tenant_persona_profiles
                    WHERE tenant_id = :tenant_id
                    """),
                {"tenant_id": tenant_id},
            )
        ).first()
        if profile is None:
            return None
        self_content = (
            await sess.execute(
                text("""
                    SELECT summary
                    FROM memory_items
                    WHERE tenant_id = :tenant_id AND memory_type = 'self'
                      AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """),
                {"tenant_id": tenant_id},
            )
        ).scalar_one_or_none()
    return PersonaSnapshot(
        tenant_id=tenant_id,
        source=str(profile[0]),
        identity=str(profile[1] or ""),
        personality_rules=str(profile[2] or ""),
        relationship_state=str(self_content or ""),
    )


class PgRelationshipIO:
    """optimizer 的 tenant RelationshipState 读写（ADR-1 seam + ADR-5 审计）。"""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def read(self, tenant_id: str) -> str:
        async with self._sf() as sess:
            value = await sess.scalar(
                text("""
                    SELECT summary
                    FROM memory_items
                    WHERE tenant_id = :tenant_id AND memory_type = 'self'
                      AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """),
                {"tenant_id": tenant_id},
            )
            return str(value or "")

    async def write(
        self, tenant_id: str, content: str, *, turn_id: str | None = None
    ) -> None:
        """单事务：self 当前值 upsert + relationship_update 审计行。

        串行性由调用方保证（optimizer per-tenant lock / tenant lane）；本方法
        保证原子与审计。detail 只记字符数，不复制正文全文（ADR-5）。
        """
        async with self._sf() as sess, sess.begin():
            row_id = await sess.scalar(
                text("""
                    SELECT id FROM memory_items
                    WHERE tenant_id = :tenant_id AND memory_type = 'self'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """),
                {"tenant_id": tenant_id},
            )
            if row_id is not None:
                await sess.execute(
                    text("""
                        UPDATE memory_items
                        SET summary = :content, content_hash = :content_hash,
                            updated_at = now()
                        WHERE id = :row_id
                        """),
                    {
                        "content": content,
                        "content_hash": str(hash(content)),
                        "row_id": row_id,
                    },
                )
            else:
                # 兜底：onboarding 种子缺失时显式补种（正常时序不应走到这里）。
                await sess.execute(
                    text("""
                        INSERT INTO memory_items
                            (id, tenant_id, memory_type, summary, content_hash,
                             reinforcement, emotional_weight, status, created_at, updated_at)
                        VALUES (:id, :tenant_id, 'self', :content, :content_hash,
                                1, 0, 'active', now(), now())
                        """),
                    {
                        "id": f"self_{uuid.uuid4().hex[:12]}",
                        "tenant_id": tenant_id,
                        "content": content,
                        "content_hash": str(hash(content)),
                    },
                )
            await sess.execute(
                text("""
                    INSERT INTO persona_audit_events
                        (tenant_id, actor, action, turn_id, detail)
                    VALUES (:tenant_id, 'optimizer', 'relationship_update', :turn_id,
                            CAST(:detail AS jsonb))
                    """),
                {
                    "tenant_id": tenant_id,
                    "turn_id": uuid.UUID(turn_id) if turn_id else None,
                    "detail": json.dumps({"chars": len(content)}),
                },
            )


def build_persona_wiring(
    session_factory: async_sessionmaker,
) -> tuple[Any, PgRelationshipIO]:
    """PG durable 装配点：返回 (snapshot resolver, relationship io)。"""
    return (
        lambda tenant_id: resolve_persona_snapshot(session_factory, tenant_id),
        PgRelationshipIO(session_factory),
    )


__all__ = ["PgRelationshipIO", "build_persona_wiring", "resolve_persona_snapshot"]
