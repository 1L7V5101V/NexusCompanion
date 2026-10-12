"""C9 persona 解析与 RelationshipState PG 写缝（c9-persona-relationship ADR-1/4/5，
c18-tenant-scoped-memory ADR-1/9/13 改目标表）。

- `resolve_persona_snapshot`：turn 入口在 tenant lane 内调用一次，取该 tenant 的
  PersonaProfile（PG 固定快照）与四格画像 + 关系状态当前值（画像专表），组成一对不可变
  快照随 ContextRequest 走 prompt 组装。无 profile（onboarding 未完成）返回 None →
  prompt 各 block 走单体回退语义。
- `PgRelationshipIO`：optimizer 的 RelationshipState 读写落 PG seam——写为
  `tenant_memory_profile_sections` 的 `relationship_state` 整行覆写 + `persona_audit_events`
  追加（同事务，ADR-5 单写者语义：串行由调用方 tenant lock / lane 保证，这里保证原子与审计）。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.core.types import MemoryProfileSnapshot, PersonaSnapshot
from agent.memory_pg import PgMemoryProfileStore


async def resolve_persona_snapshot(
    session_factory: async_sessionmaker, tenant_id: str
) -> tuple[PersonaSnapshot, MemoryProfileSnapshot] | None:
    """解析 tenant 的 (persona 快照, 记忆画像快照)；无 profile 行返回 None。

    失败-open 到单体回退语义，不阻断 turn。

    两个快照出自**同一次会话借用**（ADR-1）：两次借用之间有时间缝，并发 optimizer 写入
    会让同一轮拿到两个瞬间的拼贴。画像那两条 SELECT 的正文与「行→字段」映射都在存储层，
    这里只借会话并把会话交出去（ADR-14 末段）。
    """
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
        # `history_max_chars` 传 0（不截断）：`MemoryProfileSnapshot` 的 pending 与
        # history 今天没有任何组装期消费者（3.4 只接了 long_term 与 recent_context 两块），
        # 预算等出现消费者时按那一处定，现在不编数字。
        assembly = await PgMemoryProfileStore(
            session_factory, tenant_id
        ).read_assembly(sess, history_max_chars=0)
    return (
        PersonaSnapshot(
            tenant_id=tenant_id,
            source=str(profile[0]),
            identity=str(profile[1] or ""),
            personality_rules=str(profile[2] or ""),
            relationship_state=assembly.relationship_state,
        ),
        MemoryProfileSnapshot(
            tenant_id=tenant_id,
            long_term=assembly.long_term,
            pending=assembly.pending,
            recent_context=assembly.recent_context,
            history=assembly.history,
        ),
    )


class PgRelationshipIO:
    """optimizer 的 tenant RelationshipState 读写（ADR-9 写缝 + ADR-5 审计）。"""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    def _store(self, tenant_id: str) -> PgMemoryProfileStore:
        """按调用交来的 tenant_id 现造 store。

        store 是构造时绑租户的（6.4），本缝是每次调用绑租户的，这是本任务的一处形状错配。
        构造本身零 I/O，所以每次现造不花钱；4.1 的「租户 → 画像读写入口」解析器落地后，
        这一处收敛进解析器，改的是构造那一行而不是语义。
        """
        return PgMemoryProfileStore(self._sf, tenant_id)

    async def read(self, tenant_id: str) -> str:
        return await self._store(tenant_id).read_section("relationship_state")

    async def write(
        self, tenant_id: str, content: str, *, turn_id: str | None = None
    ) -> None:
        """单事务：`relationship_state` 整行覆写 + `relationship_update` 审计行。

        串行性由调用方保证（optimizer per-tenant lock / tenant lane）；本方法保证原子与
        审计。detail 只记字符数，不复制正文全文（ADR-5）。画像行的 upsert 走 store 并交出
        本会话，所以它不会自开事务、也不会把这笔事务提前提交。
        """
        store = self._store(tenant_id)
        async with self._sf() as sess, sess.begin():
            await store.write_section("relationship_state", content, session=sess)
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
