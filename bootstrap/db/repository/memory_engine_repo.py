"""Memory engine binding 仓储（C14：memory-engine-catalog）。

数据边界见 openspec/changes/c14-memory-engine-catalog/design.md ADR-1：每 tenant
恰一行 active binding（``tenant_id`` 主键 = 单 active 约束，§5.9.16 memory engine
slot = required）；切换是**单事务**（UPDATE binding（engine_id + revision+1）+
INSERT switch 事件），失败整体回滚不产生半状态；ensure 幂等（已存在回读，不提升
revision、不写重复 initial 事件）。本仓储只承载 binding 元数据，SHALL NOT 触碰
任何引擎数据（切换不迁移/不删除旧引擎数据）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.memory_engine import (
    TenantMemoryEngineBindingModel,
    TenantMemoryEngineEventModel,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _binding_to_dict(row: TenantMemoryEngineBindingModel) -> dict[str, Any]:
    return {
        "tenant_id": row.tenant_id,
        "engine_id": row.engine_id,
        "tenant_policy_revision": int(row.tenant_policy_revision),
        "updated_by": row.updated_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class MemoryEngineBindingRepository:
    """binding 行 / 事件历史的最小读写 seam。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    async def get_binding(self, tenant_id: str) -> dict[str, Any] | None:
        tid = _require_tenant(tenant_id)
        async with self._sf() as sess:
            row = await sess.get(TenantMemoryEngineBindingModel, tid)
            return _binding_to_dict(row) if row is not None else None

    async def list_bindings(self) -> list[dict[str, Any]]:
        async with self._sf() as sess:
            rows = (
                (await sess.execute(select(TenantMemoryEngineBindingModel)))
                .scalars()
                .all()
            )
            return [_binding_to_dict(row) for row in rows]

    async def create_initial_binding(
        self,
        *,
        tenant_id: str,
        engine_id: str,
        actor: str = "",
    ) -> dict[str, Any]:
        """创建初始绑定（幂等）：已存在则原样回读，不提升 revision。

        并发竞态以主键冲突回读收束（单 active 约束兜底）。
        """
        tid = _require_tenant(tenant_id)
        eid = _require_engine(engine_id)
        async with self._sf() as sess, sess.begin():
            existing = await sess.get(TenantMemoryEngineBindingModel, tid)
            if existing is not None:
                return _binding_to_dict(existing)
            row = TenantMemoryEngineBindingModel(
                tenant_id=tid,
                engine_id=eid,
                tenant_policy_revision=0,
                updated_by=actor[:64],
            )
            sess.add(row)
            try:
                await sess.flush()
            except IntegrityError:
                raise _BindingConflictError(
                    "memory engine binding conflict"
                ) from None
            sess.add(
                TenantMemoryEngineEventModel(
                    tenant_id=tid,
                    engine_id=eid,
                    action="initial",
                    tenant_policy_revision=0,
                    actor=actor[:64],
                )
            )
            return _binding_to_dict(row)

    async def switch_binding(
        self,
        *,
        tenant_id: str,
        engine_id: str,
        actor: str = "",
    ) -> dict[str, Any]:
        """切换 active engine（单事务）：revision +1 + switch 事件。

        返回更新后的 binding；binding 不存在时抛 :class:`_BindingMissingError`
        （调用方先 ensure）。
        """
        tid = _require_tenant(tenant_id)
        eid = _require_engine(engine_id)
        async with self._sf() as sess, sess.begin():
            row = await sess.get(TenantMemoryEngineBindingModel, tid)
            if row is None:
                raise _BindingMissingError(
                    "memory engine binding missing"
                )
            new_revision = int(row.tenant_policy_revision) + 1
            row.engine_id = eid
            row.tenant_policy_revision = new_revision
            row.updated_by = actor[:64]
            row.updated_at = _utc_now()
            sess.add(
                TenantMemoryEngineEventModel(
                    tenant_id=tid,
                    engine_id=eid,
                    action="switch",
                    tenant_policy_revision=new_revision,
                    actor=actor[:64],
                )
            )
            await sess.flush()
            return _binding_to_dict(row)

    async def list_events(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """binding 变更历史（新→旧），按 (tenant_id, engine_id) 可追踪。"""
        tid = _require_tenant(tenant_id)
        async with self._sf() as sess:
            stmt = (
                select(TenantMemoryEngineEventModel)
                .where(TenantMemoryEngineEventModel.tenant_id == tid)
                .order_by(TenantMemoryEngineEventModel.id.desc())
                .limit(max(1, min(int(limit), 500)))
            )
            rows = (await sess.execute(stmt)).scalars().all()
            return [
                {
                    "id": int(row.id),
                    "tenant_id": row.tenant_id,
                    "engine_id": row.engine_id,
                    "action": row.action,
                    "tenant_policy_revision": int(row.tenant_policy_revision),
                    "actor": row.actor,
                    "created_at": row.created_at.isoformat()
                    if row.created_at
                    else None,
                }
                for row in rows
            ]


class _BindingConflictError(RuntimeError):
    """主键竞态冲突（并发 ensure/switch）。"""


class _BindingMissingError(RuntimeError):
    """binding 行不存在（switch 前未 ensure）。"""


def _require_tenant(tenant_id: str) -> str:
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id 必填（禁止默认租户回退）")
    return tid


def _require_engine(engine_id: str) -> str:
    eid = (engine_id or "").strip()
    if not eid:
        raise ValueError("engine_id 必填")
    return eid


__all__ = ["MemoryEngineBindingRepository"]
