"""C5 auth 模型：access_tokens / auth_sessions / admin_credentials /
admin_audit_events / tenant_provisioning_jobs。

DDL 冻结于 openspec/changes/2026-09-07-c5-auth-provisioning-admin/design.md §1；
语义门禁：PILOT_ROADMAP §5.9.3（凭据 digest-only、401/403）、§5.9.9（实体清单/
唯一约束/状态枚举）、§5.9.13（provisioning 生命周期）、§10 DECIDED（单一 admin
principal、token 轮换与 session 撤销独立）。凭据只存带 pepper 的 HMAC-SHA-256
digest（64 hex），明文只允许在签发/兑换响应中出现一次。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
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

PRINCIPAL_TYPES = ("user", "admin")
"""会话主体类型：普通测试用户 / 单一 admin principal（Cookie 隔离的根）。"""

PROVISIONING_JOB_STATUSES = ("pending", "running", "ready", "failed")
"""provisioning job 状态枚举（§5.9.13）：ready/failed 为终态，running 崩溃残留可重扫。"""

DIGEST_LENGTH = 64
"""HMAC-SHA-256 hex digest 固定长度（CHECK 约束同款）。"""


class AccessTokenModel(Base):
    """一次性邀请 Token（digest-only）。

    生命周期：签发（未消费）→ `consumed_at`（原子兑换）或 `revoked_at`（撤销/
    账号封禁级联）；`expires_at` 可选过期。兑换原子性见 design ADR-2（行锁 +
    条件 UPDATE + INSERT session 同事务）。
    """

    __tablename__ = "access_tokens"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_access_tokens_digest"),
        CheckConstraint(
            f"char_length(token_digest) = {DIGEST_LENGTH}",
            name="ck_access_tokens_digest_sha256",
        ),
        Index("ix_access_tokens_account", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "test_accounts.id",
            ondelete="RESTRICT",
            name="fk_access_tokens_account_id",
        ),
        nullable=False,
    )
    token_digest: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1
    )
    display_note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    issued_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AuthSessionModel(Base):
    """登录会话（普通用户与 admin 同表，principal_type 区分；Cookie 隔离）。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("session_digest", name="uq_auth_sessions_digest"),
        CheckConstraint(
            "principal_type = 'admin' OR (principal_type = 'user' "
            "AND account_id IS NOT NULL)",
            name="ck_auth_sessions_principal",
        ),
        Index("ix_auth_sessions_account", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "test_accounts.id",
            ondelete="RESTRICT",
            name="fk_auth_sessions_account_id",
        ),
    )
    session_digest: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1
    )
    # timeout 在创建时固化（design §1）：配置后续修改只影响新会话。
    idle_timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    absolute_timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    user_agent: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AdminCredentialModel(Base):
    """单一 admin principal（§10 DECIDED：`CHECK (id = 1)` 强制单行）。

    `recovery_digest` 为空表示尚未 bootstrap；bootstrap 只允许从空 digest 创建。
    `revision` 每次 recovery token 轮换 +1（审计/排障用，不用于并发控制）。
    """

    __tablename__ = "admin_credentials"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_admin_credentials_singleton"),
        CheckConstraint(
            f"recovery_digest IS NULL OR char_length(recovery_digest) = {DIGEST_LENGTH}",
            name="ck_admin_credentials_digest_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, server_default=text("1")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recovery_digest: Mapped[str | None] = mapped_column(String(DIGEST_LENGTH))
    digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AdminAuditEventModel(Base):
    """admin 审计事件（CLI 命令与 admin HTTP 操作）。

    只允许 digest/计数/动作名；明文 recovery token / session 值禁止写入
    （§5.9.3：日志、shell history、进程参数、环境 dump、数据库和审计都不得
    出现明文 recovery token）。
    """

    __tablename__ = "admin_audit_events"
    __table_args__ = (Index("ix_admin_audit_created", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantProvisioningJobModel(Base):
    """账号 provisioning job（§5.9.13：持久、可恢复、幂等 retry）。

    pending 行尚未分配 tenant（`uq_..._tenant` 唯一索引不约束 NULL）；执行时
    先原子分配 tenant_id 再置 running。retry 沿用同一 job 行，不产生第二个
    agent（tenant 全局唯一 + create_agent 幂等兜底）。
    """

    __tablename__ = "tenant_provisioning_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_provisioning_jobs_tenant"),
        CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed')",
            name="ck_tenant_provisioning_jobs_status",
        ),
        CheckConstraint(
            "status IN ('pending', 'failed') OR tenant_id IS NOT NULL",
            name="ck_tenant_provisioning_jobs_tenant_present",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "test_accounts.id",
            ondelete="RESTRICT",
            name="fk_tenant_provisioning_jobs_account",
        ),
        nullable=False,
    )
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(
        String(32), nullable=False, default="provision_agent"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
