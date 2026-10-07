"""Memory engine catalog/binding 服务（C14：memory-engine-catalog）。

组合代码冻结目录（§5.9.16 固定表：``default`` = default_on 初始绑定、
``rachael`` = opt_in 已安装可选实现；inspector 是 admin observability
contribution，**不入目录**）与 `MemoryEngineBindingRepository`（PG 持久化），
对外提供：

- ``ensure_binding``：幂等建立初始绑定（provisioning 与懒补齐共用）；
- ``resolve_active_engine``：work-start 解析当前 active engine（缺绑定则懒补齐）；
- ``switch``：切换校验（目录内 → 用户侧放行 → engine ready）→ revision +1 →
  switch 事件；同 engine 幂等不提升；
- ``describe``：GET 端点视图（目录 + ready/active/revision）；
- ``snapshot_active``：进程内快照（供 ingest 独占门控同步读取；work-start 解析
  与 GET/PUT 均刷新缓存）。

「管理员允许」的 Pilot 形态（design ADR-2）：opt_in 引擎的放行 = 已构建（
`[memory] engine` 含该引擎，即管理员安装动作）∧ 用户侧切换总开关
（`[memory] user_engine_selection`）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Collection

from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.memory_engine_repo import (
    MemoryEngineBindingRepository,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MEMORY_ENGINE_ID",
    "EngineIngestGate",
    "EngineNotReadyError",
    "EngineNotSelectableError",
    "MEMORY_ENGINE_CATALOG",
    "MemoryEngineCatalogEntry",
    "TenantMemoryEngineBindingService",
    "UnknownEngineError",
]

DEFAULT_MEMORY_ENGINE_ID = "default"
"""memory engine slot 初始绑定（§5.9.16 provisioning 规则 3）。"""


@dataclass(frozen=True)
class MemoryEngineCatalogEntry:
    """目录项：能力描述 + binding_policy（§5.9.16 固定表冻结值）。"""

    engine_id: str
    display_name: str
    description: str
    binding_policy: str  # "default_on" | "opt_in"（slot 本身 = required）


MEMORY_ENGINE_CATALOG: tuple[MemoryEngineCatalogEntry, ...] = (
    MemoryEngineCatalogEntry(
        engine_id="default",
        display_name="default",
        description=(
            "默认记忆引擎：dense 语义 + 关键词 + RRF 融合召回，"
            "自动记忆写入与整合，开放 recall/memorize/forget 工具。"
        ),
        binding_policy="default_on",
    ),
    MemoryEngineCatalogEntry(
        engine_id="rachael",
        display_name="rachael",
        description=(
            "Rachael message-as-truth 引擎：以对话原文为真相源的增量记忆，"
            "开放 recall/reinforce_memory 工具。"
        ),
        binding_policy="opt_in",
    ),
)


class UnknownEngineError(RuntimeError):
    """engine 不在服务端目录内（客户端字段不参与授权）。"""


class EngineNotReadyError(RuntimeError):
    """engine 未在本进程构建（not ready），不可切换。"""


class EngineNotSelectableError(RuntimeError):
    """tenant policy 不允许用户侧切换（总开关关闭）。"""


class EngineIngestGate:
    """ingest 独占门控（C14 ADR-5）：多引擎并存时仅租户 active engine 处理
    TurnCommitted/ConsolidationCommitted（§5.9.16 固定表「automatic memory
    ingest/save」行）。

    reader 由 bootstrap 在 binding 服务就绪后绑定（引擎构建顺序早于 PG durable
    runtime，无法走构造参数）；未绑定/快照未命中 fail-open（记由调用方负责）——
    正常流中 work-start 解析必然先于同 turn 的 TurnCommitted 触发，快照已预热。
    单引擎构建时 bootstrap 不安装本门控，行为逐字节不变。
    """

    def __init__(self) -> None:
        self._reader = None

    def bind_reader(self, reader) -> None:
        """绑定 ``tenant_id -> engine_id | None`` 的同步快照读取函数。"""
        self._reader = reader

    def is_active(self, tenant_id: str, engine_id: str) -> bool:
        reader = self._reader
        if reader is None:
            return True
        try:
            active = reader(tenant_id)
        except Exception:
            logger.exception("memory engine ingest 门控读取失败（fail-open）")
            return True
        if active is None:
            return True
        return active == engine_id


class TenantMemoryEngineBindingService:
    """目录/binding/切换的服务层唯一入口。"""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        ready_engines: Collection[str] = (),
        user_selection_allowed: bool = True,
    ):
        self._sf = session_factory
        self._repo = MemoryEngineBindingRepository(session_factory)
        self._ready = frozenset(ready_engines)
        self._user_selection_allowed = bool(user_selection_allowed)
        # tenant_id → active engine 的进程内快照：ingest 门控同步读取；
        # resolve/ensure/switch 路径写透（Pilot 单实例，无跨进程失效需求）。
        self._snapshot: dict[str, str] = {}

    # ── 目录（只读，服务端冻结）──────────────────────────────────

    @property
    def catalog(self) -> tuple[MemoryEngineCatalogEntry, ...]:
        return MEMORY_ENGINE_CATALOG

    def is_ready(self, engine_id: str) -> bool:
        return engine_id in self._ready

    def is_selectable(self, engine_id: str) -> bool:
        """用户侧可选 = 总开关开 ∧ 引擎 ready（opt_in 的管理员放行即安装）。"""
        return self._user_selection_allowed and self.is_ready(engine_id)

    # ── binding 生命周期 ────────────────────────────────────────

    async def ensure_binding(
        self, tenant_id: str, *, actor: str = "system"
    ) -> dict:
        """幂等建立初始绑定（``default``）；已存在则原样回读。"""
        record = await self._repo.create_initial_binding(
            tenant_id=tenant_id,
            engine_id=DEFAULT_MEMORY_ENGINE_ID,
            actor=actor,
        )
        self._snapshot[str(record["tenant_id"])] = str(record["engine_id"])
        return record

    async def resolve_active_engine(self, tenant_id: str) -> str:
        """work-start 解析：读当前 active engine，缺绑定则懒补齐初始绑定。"""
        cached = self._snapshot.get(tenant_id)
        if cached is not None:
            return cached
        record = await self._repo.get_binding(tenant_id)
        if record is None:
            record = await self.ensure_binding(tenant_id)
        engine_id = str(record["engine_id"])
        self._snapshot[tenant_id] = engine_id
        return engine_id

    async def resolve_revision(self, tenant_id: str) -> int:
        """读取当前 tenant_policy_revision（供 TenantRuntimePlan 以 r<n> 消费）。"""
        record = await self._repo.get_binding(tenant_id)
        if record is None:
            record = await self.ensure_binding(tenant_id)
        return int(record["tenant_policy_revision"])

    async def get_active_engine(self, tenant_id: str) -> str:
        return await self.resolve_active_engine(tenant_id)

    async def switch(
        self,
        tenant_id: str,
        engine_id: str,
        *,
        actor: str = "user",
    ) -> dict:
        """校验并切换 active engine；同 engine 幂等（不提升 revision）。"""
        entry = next(
            (e for e in MEMORY_ENGINE_CATALOG if e.engine_id == engine_id),
            None,
        )
        if entry is None:
            raise UnknownEngineError(f"engine not in catalog: {engine_id!r}")
        if not self._user_selection_allowed:
            raise EngineNotSelectableError(
                "user-side engine selection is disabled"
            )
        if not self.is_ready(engine_id):
            raise EngineNotReadyError(f"engine not ready: {engine_id!r}")

        current = await self._repo.get_binding(tenant_id)
        if current is None:
            current = await self.ensure_binding(tenant_id)
        if str(current["engine_id"]) == engine_id:
            self._snapshot[tenant_id] = engine_id
            return current

        record = await self._repo.switch_binding(
            tenant_id=tenant_id,
            engine_id=engine_id,
            actor=actor,
        )
        self._snapshot[tenant_id] = engine_id
        logger.info(
            "memory engine 切换 tenant=%s engine=%s revision=%s",
            tenant_id,
            engine_id,
            record["tenant_policy_revision"],
        )
        return record

    async def describe(self, tenant_id: str) -> dict:
        """GET 端点视图：服务端目录 + ready/selectable/active/revision。"""
        current = await self._repo.get_binding(tenant_id)
        if current is None:
            current = await self.ensure_binding(tenant_id)
        active = str(current["engine_id"])
        return {
            "engines": [
                {
                    "engine_id": entry.engine_id,
                    "display_name": entry.display_name,
                    "description": entry.description,
                    "binding_policy": entry.binding_policy,
                    "ready": self.is_ready(entry.engine_id),
                    "selectable": self.is_selectable(entry.engine_id),
                    "active": entry.engine_id == active,
                }
                for entry in MEMORY_ENGINE_CATALOG
            ],
            "active_engine": active,
            "tenant_policy_revision": int(current["tenant_policy_revision"]),
        }

    async def list_events(self, tenant_id: str, *, limit: int = 50) -> list[dict]:
        """binding 变更历史（initial/switch），按 (tenant_id, engine_id) 可追踪。"""
        return await self._repo.list_events(tenant_id, limit=limit)

    # ── ingest 门控快照（同步读）────────────────────────────────

    def snapshot_active(self, tenant_id: str) -> str | None:
        """进程内快照读取；未命中返回 None（调用方 fail-open 并记日志）。"""
        return self._snapshot.get(tenant_id)
