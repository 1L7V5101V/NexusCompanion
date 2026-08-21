"""StorageRuntime / TenantStorage seam（M4H-2 C）。

进程级 StorageRuntime（bootstrap 创建一次）→ for_tenant(TenantContext) →
turn/request-scoped 的 TenantStorage 轻量 view。view 只绑定 TenantContext + 共享
backend（或 sqlite 共享 store）的引用，不拥有连接、不暴露 close()；底层生命
周期只归 StorageRuntime.close()。

- postgres：每 for_tenant 返回共享 backend 上的廉价 view（不建连接/不缓存
  per-tenant 单例）。
- sqlite：显式 single-user，for_tenant 忽略 tenant、返回同一对共享 store。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from infra.storage.interfaces import MemoryStorage, SessionStorage, TenantContext
from infra.storage.postgres_memory_store import (
    PostgresMemoryBackend,
    PostgresMemoryStore,
)
from infra.storage.postgres_session_store import (
    PostgresSessionBackend,
    PostgresSessionStore,
)
from memory2.store import VEC_DIM, MemoryStore2
from session.store import SessionStore


@dataclass(frozen=True)
class TenantStorage:
    """turn/request-scoped 的 tenant-bound 轻量 view。

    只绑定 TenantContext，不拥有连接/backend，不暴露 close；底层关闭归
    StorageRuntime.close()。
    """

    tenant: TenantContext
    memory: MemoryStorage
    sessions: SessionStorage


class StorageRuntime:
    """进程级存储运行时：bootstrap 创建一次，for_tenant 出 tenant-bound view。

    for_tenant() 必须廉价：不建连接、不建 per-tenant 单例，只包一层 view。
    """

    def __init__(
        self,
        postgres_url: str | None,
        memory_path: str | Path,
        sessions_path: str | Path,
        *,
        vec_dim: int = VEC_DIM,
        pool_size: int = 20,
    ) -> None:
        self._pool_size = pool_size
        if postgres_url:
            self._mode: Literal["postgres", "sqlite"] = "postgres"
            self._memory_backend: PostgresMemoryBackend | None = (
                PostgresMemoryBackend(
                    postgres_url, vec_dim=vec_dim, pool_size=pool_size
                )
            )
            self._session_backend: PostgresSessionBackend | None = (
                PostgresSessionBackend(postgres_url, pool_size=pool_size)
            )
            self._memory_sqlite: MemoryStore2 | None = None
            self._session_sqlite: SessionStore | None = None
            # bounded executor：DB 调用移出 event loop（M4H-3 commit 4 接入）。
            # 线程数取 pool_size（单进程 worker_replicas=1，预算见 ADR §1.3）。
            self._executor = ThreadPoolExecutor(
                max_workers=pool_size, thread_name_prefix="pg-db"
            )
        else:
            self._mode = "sqlite"
            self._memory_backend = None
            self._session_backend = None
            self._memory_sqlite = MemoryStore2(memory_path, vec_dim=vec_dim)
            self._session_sqlite = SessionStore(sessions_path)
            self._executor = None
        self._closed = False

    def for_tenant(self, tenant: TenantContext) -> TenantStorage:
        if self._closed:
            raise RuntimeError("StorageRuntime is closed")
        if self._mode == "sqlite":
            memory = self._memory_sqlite
            sessions = self._session_sqlite
            assert memory is not None and sessions is not None
            return TenantStorage(tenant=tenant, memory=memory, sessions=sessions)
        memory_backend = self._memory_backend
        session_backend = self._session_backend
        assert memory_backend is not None and session_backend is not None
        return TenantStorage(
            tenant=tenant,
            memory=PostgresMemoryStore(memory_backend, tenant.tenant_id),
            sessions=PostgresSessionStore(session_backend, tenant.tenant_id),
        )

    def close(self) -> None:
        if self._closed:
            return
        try:
            # 先停 executor（等运行中的 DB 调用完成、拒绝新提交）→ 再关 pool
            if self._executor is not None:
                self._executor.shutdown(wait=True)
            if self._memory_backend is not None:
                self._memory_backend.close()
            if self._session_backend is not None:
                self._session_backend.close()
            if self._memory_sqlite is not None:
                self._memory_sqlite.close()
            if self._session_sqlite is not None:
                self._session_sqlite.close()
        finally:
            self._closed = True
