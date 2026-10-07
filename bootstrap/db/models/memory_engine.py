"""Memory engine binding 模型（C14：memory-engine-catalog）。

约束命名冻结于 openspec/changes/c14-memory-engine-catalog/design.md ADR-1：
每 tenant 始终有且只有一个 active memory engine（§5.9.16 memory engine slot =
required）——由 ``tenant_memory_engine_bindings.tenant_id`` **主键**直接强制；
``tenant_policy_revision`` 从 0 起、每次成功切换 +1。切换历史（initial/switch）
落 events 表按 ``(tenant_id, engine_id)`` 可追踪；本模块不承载任何引擎数据本身
（切换不迁移/不删除旧引擎数据）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bootstrap.db.models.base import Base

ENGINE_ACTIONS = ("initial", "switch")
"""binding 事件类型：provisioning/懒补齐建初始绑定 / 用户或管理员切换。"""


class TenantMemoryEngineBindingModel(Base):
    """每 tenant 恰一行的 active memory engine binding。

    ``tenant_id`` 主键即单 active 约束（一行 = 一个 active engine）；无法用
    「多行 + 部分唯一索引」表达，因为 slot 语义不允许 unbound 历史行——历史
    追踪由 ``TenantMemoryEngineEventModel`` 承担。
    """

    __tablename__ = "tenant_memory_engine_bindings"
    __table_args__ = (
        CheckConstraint("engine_id <> ''", name="ck_tmeb_engine_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    engine_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_policy_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantMemoryEngineEventModel(Base):
    """binding 变更历史：initial（首次建立）/ switch（成功切换）。

    只追加不改写；按 ``(tenant_id, engine_id, created_at)`` 查询。
    """

    __tablename__ = "tenant_memory_engine_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('initial', 'switch')", name="ck_tmee_action"
        ),
        Index("ix_tmee_tenant_engine", "tenant_id", "engine_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "ENGINE_ACTIONS",
    "TenantMemoryEngineBindingModel",
    "TenantMemoryEngineEventModel",
]
