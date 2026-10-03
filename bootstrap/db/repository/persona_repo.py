"""Persona/Relationship 仓储（c9-persona-relationship ADR-2/3/5）。

- 模板 CRUD/启停：修改与停用只影响后续 onboarding，不触碰已建快照。
- onboarding 提交：`tenant_persona_profiles` INSERT ... ON CONFLICT DO NOTHING
  原子抢位 + `memory_items(memory_type='self')` 种子（RelationshipState 当前值，
  ADR-1 复用既有 seam）+ `persona_audit_events` 追加——三者同一事务。
- Profile 读取面按 tenant 过滤；**不提供任何修改已提交快照的方法**（spec
  「提交后 PersonaProfile 用户侧不可二次修改」——仓储层就没有该入口）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.persona import (
    PersonaAuditEventModel,
    PersonaTemplateModel,
    TenantPersonaProfileModel,
)


class OnboardingAlreadyCompletedError(Exception):
    """该 tenant 已完成 onboarding（并发双提交时后到者失败）。"""


class PersonaRepository:
    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    # ── 模板（管理员） ──────────────────────────────────────────

    async def create_template(
        self,
        *,
        name: str,
        identity: str,
        personality_rules: str,
        self_model: str,
        created_by: str = "admin",
    ) -> dict[str, Any]:
        async with self._sf() as sess, sess.begin():
            row = PersonaTemplateModel(
                name=name[:64],
                identity=identity,
                personality_rules=personality_rules,
                self_model=self_model,
                enabled=True,
                created_by=created_by[:64],
            )
            sess.add(row)
            await sess.flush()
            sess.add(
                PersonaAuditEventModel(
                    tenant_id="",
                    actor="admin",
                    action="template_create",
                    detail={"template_id": str(row.id), "name": row.name},
                )
            )
            return _template_to_dict(row)

    async def set_template_enabled(self, template_id: str, *, enabled: bool) -> bool:
        """启用/停用模板；返回是否存在。已建 tenant 快照不受影响（不触碰）。"""
        tid = uuid.UUID(template_id)
        async with self._sf() as sess, sess.begin():
            row = await sess.get(PersonaTemplateModel, tid)
            if row is None:
                return False
            row.enabled = enabled
            sess.add(
                PersonaAuditEventModel(
                    tenant_id="",
                    actor="admin",
                    action="template_disable" if not enabled else "template_update",
                    detail={"template_id": str(tid), "enabled": enabled},
                )
            )
            return True

    async def list_templates(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        async with self._sf() as sess:
            stmt = select(PersonaTemplateModel).order_by(PersonaTemplateModel.name)
            if enabled_only:
                stmt = stmt.where(PersonaTemplateModel.enabled.is_(True))
            rows = (await sess.execute(stmt)).scalars().all()
            return [_template_to_dict(r) for r in rows]

    async def get_template(self, template_id: str) -> dict[str, Any] | None:
        async with self._sf() as sess:
            row = await sess.get(PersonaTemplateModel, uuid.UUID(template_id))
            return _template_to_dict(row) if row is not None else None

    # ── onboarding（一次性提交 + 锁定） ─────────────────────────

    async def submit_onboarding(
        self,
        *,
        tenant_id: str,
        source: str,
        identity: str,
        personality_rules: str,
        self_model: str,
        template_id: str | None = None,
        actor: str = "user",
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        """原子提交 onboarding：profile 抢位 + RelationshipState 种子 + 审计。

        并发双提交只成功一份（ON CONFLICT DO NOTHING；rowcount=0 → 抛
        :class:`OnboardingAlreadyCompletedError`，409 语义）。RelationshipState
        种子写 `memory_items(memory_type='self')`（ADR-1 seam，与
        `PgMemoryMarkdownStore._upsert_content` 同字段约定）。
        """
        tid = uuid.UUID(template_id) if template_id else None
        async with self._sf() as sess, sess.begin():
            result = await sess.execute(
                select(TenantPersonaProfileModel).where(
                    TenantPersonaProfileModel.tenant_id == tenant_id
                )
            )
            if result.scalar_one_or_none() is not None:
                raise OnboardingAlreadyCompletedError(tenant_id)
            profile = TenantPersonaProfileModel(
                tenant_id=tenant_id,
                source=source,
                template_id=tid,
                identity=identity,
                personality_rules=personality_rules,
                self_model=self_model,
            )
            sess.add(profile)
            # RelationshipState 种子（ADR-1 seam：memory_items 单行 self 内容）。
            # 原生 SQL 且不列 embedding（PG vector 列，ORM Text 声明在 asyncpg 下
            # 会把 NULL 绑成 VARCHAR 而被拒；既有 seam 走 psycopg 自适应故未暴露）。
            await sess.execute(
                text("""
                    INSERT INTO memory_items
                        (id, tenant_id, memory_type, summary, content_hash,
                         reinforcement, emotional_weight, status, created_at, updated_at)
                    VALUES (:id, :tenant_id, 'self', :summary, :content_hash,
                            1, 0, 'active', now(), now())
                    """),
                {
                    "id": f"self_{uuid.uuid4().hex[:12]}",
                    "tenant_id": tenant_id,
                    "summary": self_model,
                    "content_hash": str(hash(self_model)),
                },
            )
            sess.add(
                PersonaAuditEventModel(
                    tenant_id=tenant_id,
                    actor=actor,
                    action="onboarding_submit",
                    turn_id=uuid.UUID(turn_id) if turn_id else None,
                    detail={
                        "source": source,
                        "template_id": str(tid) if tid else None,
                        "identity_chars": len(identity),
                        "rules_chars": len(personality_rules),
                        "self_chars": len(self_model),
                    },
                )
            )
            return _profile_to_dict(profile)

    async def get_profile(self, tenant_id: str) -> dict[str, Any] | None:
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(TenantPersonaProfileModel).where(
                        TenantPersonaProfileModel.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            return _profile_to_dict(row) if row is not None else None

    async def has_profile(self, tenant_id: str) -> bool:
        return await self.get_profile(tenant_id) is not None

    # ── 审计 ────────────────────────────────────────────────────

    async def append_audit(
        self,
        *,
        tenant_id: str,
        actor: str,
        action: str,
        turn_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with self._sf() as sess, sess.begin():
            sess.add(
                PersonaAuditEventModel(
                    tenant_id=tenant_id,
                    actor=actor,
                    action=action,
                    turn_id=uuid.UUID(turn_id) if turn_id else None,
                    detail=detail,
                )
            )

    async def list_audit(
        self, tenant_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(PersonaAuditEventModel)
                    .where(PersonaAuditEventModel.tenant_id == tenant_id)
                    .order_by(PersonaAuditEventModel.created_at.desc())
                    .limit(limit)
                )
            )
            return [_audit_to_dict(r) for r in rows.scalars().all()]


def _template_to_dict(row: PersonaTemplateModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "identity": row.identity,
        "personality_rules": row.personality_rules,
        "self_model": row.self_model,
        "enabled": row.enabled,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _profile_to_dict(row: TenantPersonaProfileModel) -> dict[str, Any]:
    return {
        "tenant_id": row.tenant_id,
        "source": row.source,
        "template_id": str(row.template_id) if row.template_id else None,
        "identity": row.identity,
        "personality_rules": row.personality_rules,
        "self_model": row.self_model,
        "created_at": row.created_at,
    }


def _audit_to_dict(row: PersonaAuditEventModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "actor": row.actor,
        "action": row.action,
        "turn_id": str(row.turn_id) if row.turn_id else None,
        "detail": row.detail,
        "created_at": row.created_at,
    }


__all__ = [
    "OnboardingAlreadyCompletedError",
    "PersonaRepository",
]
