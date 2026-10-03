"""C9 Persona/Relationship：persona_templates / tenant_persona_profiles /
persona_audit_events 三表（c9-persona-relationship ADR-2）

- `persona_templates`：管理员可选 Persona 模板（identity/personality_rules/
  self_model 三块自由文本，沿用当前单体语义）；修改/停用只影响后续 onboarding。
- `tenant_persona_profiles`：onboarding 提交后的**固定快照**——UNIQUE(tenant_id)
  即「提交后固定」的 schema 表达；只有 created_at 无 updated_at（不可变行）。
  source CHECK ∈ ('template','custom')；template_id 仅 source='template' 时非空
  （应用层保证，FK SET NULL 保留快照内容不随模板删除失效）。
- `persona_audit_events`：Persona 写入审计（actor/action/turn_id/detail），
  detail 只记 metadata 摘要不复制正文全文；供 admin 下钻与 C12 观测复用。

RelationshipState 不建独立表：复用 `memory_items(memory_type='self')` 既有
tenant-scoped content seam（c9-persona-relationship ADR-1，roadmap §5.7.1
「具体表名可调整」条款）。**不建**任何 revision/version/history 表（spec
「PersonaProfile 提交后固定」——无版本表 Scenario）。

纯 expand-only：downgrade 只 drop 三表，不动任何既有表。

Revision ID: e1f3a5c7b9d2
Revises: d0a9b7c3e1f5
Create Date: 2026-10-04
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e1f3a5c7b9d2"
down_revision: Union[str, Sequence[str], None] = "d0a9b7c3e1f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE persona_templates (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                VARCHAR(64)  NOT NULL,
            identity            TEXT         NOT NULL,
            personality_rules   TEXT         NOT NULL,
            self_model          TEXT         NOT NULL,
            enabled             BOOLEAN      NOT NULL DEFAULT TRUE,
            created_by          VARCHAR(64)  NOT NULL DEFAULT '',
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_persona_templates_name UNIQUE (name),
            CONSTRAINT ck_persona_templates_enabled CHECK (enabled IN (TRUE, FALSE))
        )
        """)
    op.execute("""
        CREATE TABLE tenant_persona_profiles (
            tenant_id           VARCHAR(64)  PRIMARY KEY,
            source              VARCHAR(16)  NOT NULL,
            template_id         UUID         NULL,
            identity            TEXT         NOT NULL,
            personality_rules   TEXT         NOT NULL,
            self_model          TEXT         NOT NULL,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_tenant_persona_profiles_source
                CHECK (source IN ('template', 'custom')),
            CONSTRAINT fk_tenant_persona_profiles_template_id
                FOREIGN KEY (template_id)
                REFERENCES persona_templates (id) ON DELETE SET NULL
        )
        """)
    op.execute("""
        CREATE TABLE persona_audit_events (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           VARCHAR(64)  NOT NULL,
            actor               VARCHAR(16)  NOT NULL,
            action              VARCHAR(32)  NOT NULL,
            turn_id             UUID         NULL,
            detail              JSONB        NULL,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_persona_audit_events_actor
                CHECK (actor IN ('user', 'optimizer', 'admin')),
            CONSTRAINT ck_persona_audit_events_action
                CHECK (action IN ('onboarding_submit', 'relationship_update',
                                  'template_create', 'template_update',
                                  'template_disable'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_persona_audit_tenant_created
            ON persona_audit_events (tenant_id, created_at)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS persona_audit_events")
    op.execute("DROP TABLE IF EXISTS tenant_persona_profiles")
    op.execute("DROP TABLE IF EXISTS persona_templates")
