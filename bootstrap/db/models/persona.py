"""C9 Persona/Relationship 模型：persona_templates / tenant_persona_profiles /
persona_audit_events（c9-persona-relationship ADR-2）。

DDL 冻结于 openspec/changes/c9-persona-relationship/design.md ADR-2；语义门禁：
PILOT_ROADMAP §5.7（配置分层/SELF.md 迁移/生效时机）、§5.9.8、§10 DECIDED
（Persona/Relationship 存储语义：PersonaProfile 提交后固定、RelationshipState
单写者原地更新、无 revision 链）。RelationshipState 本体复用
`memory_items(memory_type='self')`（ADR-1），不在本模块。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base

PERSONA_SOURCES = ("template", "custom")
"""onboarding 来源：管理员模板选择 / 用户自由文本。"""

PERSONA_ACTORS = ("user", "optimizer", "admin")
"""Persona 写入审计的执行方。"""

PERSONA_ACTIONS = (
    "onboarding_submit",
    "relationship_update",
    "template_create",
    "template_update",
    "template_disable",
)


class PersonaTemplateModel(Base):
    """管理员可选 Persona 模板；修改/停用只影响后续 onboarding，不覆盖已建快照。"""

    __tablename__ = "persona_templates"
    __table_args__ = (
        UniqueConstraint("name", name="uq_persona_templates_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    personality_rules: Mapped[str] = mapped_column(Text, nullable=False)
    self_model: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantPersonaProfileModel(Base):
    """tenant 一次性 onboarding 提交后的固定快照（current-state，不可变行）。

    只有 created_at 无 updated_at：提交后任何路径 SHALL NOT 修改正文
    （spec「PersonaProfile 提交后固定」）。template_id 为软引用（SET NULL）：
    模板删除不影响已提交快照内容。
    """

    __tablename__ = "tenant_persona_profiles"
    __table_args__ = (
        CheckConstraint(
            "source IN ('template', 'custom')",
            name="ck_tenant_persona_profiles_source",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        # 软引用：SET NULL 保快照内容；避免与 templates 硬生命周期耦合。
        # 应用层保证 source='template' 时非空。
    )
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    personality_rules: Mapped[str] = mapped_column(Text, nullable=False)
    self_model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonaAuditEventModel(Base):
    """Persona 写入审计（onboarding 提交 / RelationshipState 更新 / 模板管理）。

    detail 只放 metadata 摘要（来源、触发 turn、字符数），不复制正文全文
    （design ADR-5）；供 admin 下钻与 C12 观测复用。
    """

    __tablename__ = "persona_audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor IN ('user', 'optimizer', 'admin')",
            name="ck_persona_audit_events_actor",
        ),
        CheckConstraint(
            "action IN ('onboarding_submit', 'relationship_update', "
            "'template_create', 'template_update', 'template_disable')",
            name="ck_persona_audit_events_action",
        ),
        Index("ix_persona_audit_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
