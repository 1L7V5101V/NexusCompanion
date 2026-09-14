"""C5 auth/provisioning tables

access_tokens / auth_sessions / admin_credentials / admin_audit_events /
tenant_provisioning_jobs 五表（openspec/changes/2026-09-07-c5-auth-provisioning-
admin/design.md §1 冻结 DDL）。

语义门禁：PILOT_ROADMAP §5.9.3（digest-only、单一 admin principal、Admin
bootstrap）、§5.9.9（实体清单/唯一约束/状态枚举）、§5.9.13（provisioning job
持久可恢复）、§10 DECIDED。seed：not_applicable（admin 行由 `pilot-admin
bootstrap` 显式创建，见 design §1 注记）。

首次启用 = Create → Verify → Enable；C5 尚未 cutover，downgrade 直接 DROP
五张新表是安全操作（§5.9.9）。

Revision ID: b7e2f9a4c1d8
Revises: f3c8a9d2e7b4
Create Date: 2026-09-07
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b7e2f9a4c1d8"
down_revision: Union[str, Sequence[str], None] = "f3c8a9d2e7b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE access_tokens (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id     UUID         NOT NULL,
            token_digest   VARCHAR(64)  NOT NULL,
            digest_version SMALLINT     NOT NULL DEFAULT 1,
            display_note   VARCHAR(255) NOT NULL DEFAULT '',
            issued_by      VARCHAR(64)  NOT NULL DEFAULT '',
            expires_at     TIMESTAMPTZ  NULL,
            consumed_at    TIMESTAMPTZ  NULL,
            revoked_at     TIMESTAMPTZ  NULL,
            revoked_reason VARCHAR(255) NOT NULL DEFAULT '',
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_access_tokens_digest UNIQUE (token_digest),
            CONSTRAINT fk_access_tokens_account_id
                FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT,
            CONSTRAINT ck_access_tokens_digest_sha256
                CHECK (char_length(token_digest) = 64)
        )
        """)
    op.execute(
        "CREATE INDEX ix_access_tokens_account ON access_tokens (account_id)"
    )
    op.execute("""
        CREATE TABLE auth_sessions (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_type     VARCHAR(16)  NOT NULL,
            account_id         UUID         NULL,
            session_digest     VARCHAR(64)  NOT NULL,
            digest_version     SMALLINT     NOT NULL DEFAULT 1,
            idle_timeout_s     INTEGER      NOT NULL,
            absolute_timeout_s INTEGER      NOT NULL,
            user_agent         VARCHAR(255) NOT NULL DEFAULT '',
            last_seen_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            expires_at         TIMESTAMPTZ  NOT NULL,
            revoked_at         TIMESTAMPTZ  NULL,
            revoked_reason     VARCHAR(255) NOT NULL DEFAULT '',
            created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_auth_sessions_digest UNIQUE (session_digest),
            CONSTRAINT ck_auth_sessions_principal CHECK (
                principal_type = 'admin'
                OR (principal_type = 'user' AND account_id IS NOT NULL)),
            CONSTRAINT fk_auth_sessions_account_id
                FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT
        )
        """)
    op.execute(
        "CREATE INDEX ix_auth_sessions_account ON auth_sessions (account_id)"
    )
    op.execute("""
        CREATE TABLE admin_credentials (
            id              SMALLINT PRIMARY KEY DEFAULT 1,
            enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
            recovery_digest VARCHAR(64) NULL,
            digest_version  SMALLINT    NOT NULL DEFAULT 1,
            revision        INTEGER     NOT NULL DEFAULT 0,
            rotated_at      TIMESTAMPTZ NULL,
            disabled_at     TIMESTAMPTZ NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_admin_credentials_singleton CHECK (id = 1),
            CONSTRAINT ck_admin_credentials_digest_sha256 CHECK (
                recovery_digest IS NULL OR char_length(recovery_digest) = 64)
        )
        """)
    op.execute("""
        CREATE TABLE admin_audit_events (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            actor       VARCHAR(64) NOT NULL,
            action      VARCHAR(64) NOT NULL,
            target_type VARCHAR(32) NOT NULL DEFAULT '',
            target_id   VARCHAR(64) NOT NULL DEFAULT '',
            detail      JSONB       NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """)
    op.execute(
        "CREATE INDEX ix_admin_audit_created ON admin_audit_events (created_at)"
    )
    op.execute("""
        CREATE TABLE tenant_provisioning_jobs (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id    UUID        NOT NULL,
            tenant_id     VARCHAR(64) NULL,
            operation     VARCHAR(32) NOT NULL DEFAULT 'provision_agent',
            status        VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempt_count INTEGER     NOT NULL DEFAULT 0,
            last_error    TEXT        NULL,
            started_at    TIMESTAMPTZ NULL,
            finished_at   TIMESTAMPTZ NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_tenant_provisioning_jobs_tenant UNIQUE (tenant_id),
            CONSTRAINT ck_tenant_provisioning_jobs_status
                CHECK (status IN ('pending', 'running', 'ready', 'failed')),
            CONSTRAINT ck_tenant_provisioning_jobs_tenant_present
                CHECK (status IN ('pending', 'failed') OR tenant_id IS NOT NULL),
            CONSTRAINT fk_tenant_provisioning_jobs_account
                FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT
        )
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_provisioning_jobs")
    op.execute("DROP TABLE IF EXISTS admin_audit_events")
    op.execute("DROP TABLE IF EXISTS admin_credentials")
    op.execute("DROP TABLE IF EXISTS auth_sessions")
    op.execute("DROP TABLE IF EXISTS access_tokens")
